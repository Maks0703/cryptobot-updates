#!/usr/bin/env python3
"""Фьючерсный бот для ДЕМО-счёта Bybit (Demo Trading, api-demo.bybit.com). ТОЛЬКО ДЕМО, режима live НЕТ.

Режим по умолчанию — АКТИВНЫЙ (config.yaml -> futures_strategy -> mode: active), см. active_strategy.py:
  тренд 4h (EMA50/200) задаёт направление; вход на пробое максимума/минимума последних 30 свечей 4h
  по тренду; стоп 3 ATR и тейк 4 ATR ставятся на бирже; выход по времени через 30 свечей (5 дней);
  риск 1% на сделку, плечо до 3x, до 4 позиций. В среднем ~0.7 сделки в день по 8 монетам (бэктест).
Плюс фильтр режима рынка (regime_filter.py) и аварийный стоп по просадке (circuit_breaker.py).
Монеты, риск, число позиций и «сделка дня» задаются в config.yaml (futures_strategy: coins, profile,
risk_pct, max_positions, daily_min_trade). Профиль aggressive разрешает риск до 10% (только демо),
обычный — до 2%. Монеты, которых нет на Bybit, пропускаются. Объём округляется по qtyStep/minQty/minNotional биржи.

Старый режим mode: ema_cross (как kraken_futures_bot.py / alts_short_backtest.py "EMA (50,200)", лонг и шорт):
  шорт-условие = EMA50 < EMA200 и close < EMA200 ; лонг-условие = EMA50 > EMA200 и close > EMA200
  вход   = условие только что включилось на ЗАКРЫТОЙ 4h свече (только свежий сигнал)
  выход  = условие выключилось на последней закрытой 4h свече -> закрытие по рынку
  стоп   = вход -/+ 5*ATR(14), ставится на бирже (stopLoss), подтягивается каждый прогон (только ужесточение)
  размер = 2% риска от капитала / расстояние до стопа, плечо не больше 3x, не больше 4 позиций,
           общий объём позиций не больше 3x капитала, одна позиция на монету.
Монеты: ETH SOL XRP BNB DOGE ADA AVAX LINK, бессрочные USDT-контракты (category=linear, one-way, positionIdx=0).

  py -3.12 bybit_futures_bot.py --once     # один прогон
  py -3.12 bybit_futures_bot.py --loop     # работать постоянно (прогон каждый час в ~:05)
Ключи: переменные окружения BYBIT_API_KEY / BYBIT_API_SECRET (ключ, созданный в режиме Демо-торговли).
"""
import argparse, csv, json, os, sys, time, traceback
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import pandas as pd
import regime_filter, circuit_breaker, bybit_time, active_strategy, pre_trade_gate

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(BASE, "state", "bybit_futures_state.json")
NEWS_F = os.path.join(BASE, "state", "news_gate.json")   # необязательный файл; нет файла = торговать как обычно
JOURNAL_F = os.path.join(BASE, "journal", "bybit_futures_journal.csv")
BREAKER_F = os.path.join(BASE, "state", "circuit_breaker.json")   # пик капитала для аварийного стопа
BREAKER_RESET_F = os.path.join(BASE, circuit_breaker.RESET_FILE_NAME)
CONFIG_F = os.path.join(BASE, "config.yaml")
REPORT_F = os.path.join(BASE, "отчёт.csv")   # история баланса, открывается в Excel
REPORT_COLS = ["time", "equity", "usdt", "unrealized_pnl", "open_positions", "change_24h", "btc_value"]
DEFAULT_COINS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK"]
COINS = list(DEFAULT_COINS)   # список берётся из config.yaml -> futures_strategy -> coins
MAX_COINS, MAX_POS_CAP = 40, 8
FAST, SLOW, ATR_N, STOP_K = 50, 200, 14, 5.0
RISK, LEV, MAX_POS, TOTAL_NOTIONAL_X = 0.02, 3, 4, 3.0
ACTIVE_RISK = 0.01            # риск на сделку в активном режиме (стоп ближе, сделок больше)
BAR_MS = 14_400_000
CAT = "linear"
TAG = "[DEMO-FUT]"
COLS = ["ts_utc", "coin", "symbol", "direction", "action", "reason", "bar_time", "close", "ema50", "ema200",
        "atr", "price", "qty", "notional", "stop", "equity", "pnl_usd", "pnl_pct_equity", "outcome", "order_id"]


def sym(c): return f"{c}USDT"
def coin_of(s): return s[:-4] if s.endswith("USDT") else s
def now_utc(): return datetime.now(timezone.utc)
def bar_iso(ms): return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="minutes")
def log(msg): print(f"{TAG} {datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}", flush=True)
def f(x, default=0.0):
    try: return float(x) if x not in (None, "") else default
    except (TypeError, ValueError): return default
def lst(resp):
    """Безопасно достаёт result.list (на демо-счёте списки бывают пустыми)."""
    try: return (resp or {}).get("result", {}).get("list", []) or []
    except AttributeError: return []
def err_code(e): return getattr(e, "status_code", None)


# ---------------- клиент (только демо) ----------------
def make_client():
    import keys; key, secret = keys.load()
    if not key or not secret:
        sys.exit("Нет API-ключей. Включите на bybit.com режим «Демо-торговля», создайте там API-ключ "
                 "(права: Контракты/Деривативы — Ордера и Позиции, БЕЗ вывода средств) и задайте "
                 "BYBIT_API_KEY и BYBIT_API_SECRET.")
    from pybit.unified_trading import HTTP
    c = HTTP(testnet=False, demo=True, tld="com", api_key=key, api_secret=secret, recv_window=20000)
    # жёсткая защита: работаем только с демо-сервером
    if not getattr(c, "demo", False) or "api-demo." not in str(getattr(c, "endpoint", "")):
        sys.exit(f"ОТКАЗ: клиент не в демо-режиме ({getattr(c, 'endpoint', '?')}). Этот бот работает только на демо-счёте.")
    bybit_time.install(c, log=log)   # часы ПК могут спешить -> ErrCode 10002; берём время сервера Bybit
    return c


# ---------------- состояние и журнал ----------------
def load_state():
    try:
        with open(STATE_F, encoding="utf-8") as fh: s = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError): s = {}
    s.setdefault("positions", {}); s.setdefault("last_bar", {})
    return s

def save_state(s):
    tmp = STATE_F + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh: json.dump(s, fh, indent=1)
    os.replace(tmp, STATE_F)

def journal(row):
    new = not os.path.exists(JOURNAL_F)
    with open(JOURNAL_F, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        if new: w.writeheader()
        r = {k: "" for k in COLS}; r.update({k: v for k, v in row.items() if k in COLS})
        r["ts_utc"] = now_utc().isoformat(timespec="seconds")
        for k, v in r.items():
            if isinstance(v, float): r[k] = f"{v:.6g}" if abs(v) < 1 else round(v, 4)
        w.writerow(r)

def news_gate():
    g = {"block_new_entries": False, "size_mult": 1.0}
    try:
        with open(NEWS_F, encoding="utf-8") as fh: g.update(json.load(fh))
        g["size_mult"] = min(max(f(g.get("size_mult"), 1.0), 0.0), 1.0)
        if g.get("updated"):
            age = now_utc() - datetime.fromisoformat(str(g["updated"]).replace("Z", "+00:00"))
            if age > timedelta(hours=26): g = {"block_new_entries": False, "size_mult": 1.0}  # устарел -> игнор
    except Exception: pass
    return g


def strategy_cfg():
    """Раздел futures_strategy из config.yaml: mode active (по умолчанию) или ema_cross (старая стратегия)."""
    out = {"mode": "active", "risk_pct": ACTIVE_RISK * 100, "active": {}, "profile": "normal",
           "coins": list(DEFAULT_COINS), "max_positions": 4, "daily_min_trade": False}
    try:
        import yaml, re
        with open(CONFIG_F, encoding="utf-8") as fh: c = (yaml.safe_load(fh) or {}).get("futures_strategy") or {}
        if str(c.get("mode", "active")).strip() in ("active", "ema_cross"): out["mode"] = str(c.get("mode", "active")).strip()
        out["profile"] = "aggressive" if str(c.get("profile", "normal")).strip() == "aggressive" else "normal"
        cap = 10.0 if out["profile"] == "aggressive" else 2.0     # жёстко: обычный профиль до 2%, агрессивный до 10%
        out["risk_pct"] = min(max(f(c.get("risk_pct"), ACTIVE_RISK * 100), 0.1), cap)
        out["active"] = c.get("active") or {}
        coins = [str(x).strip().upper() for x in (c.get("coins") or []) if re.fullmatch(r"[A-Z0-9]{2,20}", str(x).strip().upper())]
        coins = [x[:-4] if x.endswith("USDT") else x for x in coins]
        if coins: out["coins"] = list(dict.fromkeys(coins))[:MAX_COINS]
        out["max_positions"] = int(min(max(f(c.get("max_positions"), 4), 1), MAX_POS_CAP))
        out["daily_min_trade"] = bool(c.get("daily_min_trade", False))
    except FileNotFoundError: pass
    except Exception as e: log(f"config.yaml: раздел futures_strategy не прочитан ({str(e)[:80]}), активный режим по умолчанию")
    return out


def protection_cfg():
    """Раздел futures_protection из config.yaml. Нет раздела/файла/ошибка -> значения по умолчанию из модулей."""
    try:
        import yaml
        with open(CONFIG_F, encoding="utf-8") as fh: c = yaml.safe_load(fh) or {}
        fp = c.get("futures_protection") or {}
        return fp.get("regime_filter") or {}, fp.get("circuit_breaker") or {}
    except FileNotFoundError:
        return {}, {}
    except Exception as e:
        log(f"config.yaml: раздел futures_protection не прочитан ({str(e)[:80]}), беру настройки по умолчанию")
        return {}, {}


def gate_cfg():
    """futures_protection -> pre_trade_gate из config.yaml (нет -> значения по умолчанию, проверка включена)."""
    try:
        import yaml
        with open(CONFIG_F, encoding="utf-8") as fh: c = yaml.safe_load(fh) or {}
        g = (c.get("futures_protection") or {}).get("pre_trade_gate")
        return g if isinstance(g, dict) else {}
    except Exception:
        return {}


# ---------------- биржа ----------------
def candles(cl, coin):
    k = lst(cl.get_kline(category=CAT, symbol=sym(coin), interval="240", limit=1000))
    if len(k) < SLOW + 5: raise RuntimeError(f"мало свечей ({len(k)})")
    d = pd.DataFrame([r[:6] for r in k], columns=["t", "o", "h", "l", "c", "v"]).astype(float)
    d = d.sort_values("t").drop_duplicates("t").reset_index(drop=True)   # Bybit отдаёт от новых к старым
    d = d[d.t + BAR_MS <= time.time() * 1000].reset_index(drop=True)    # только закрытые свечи
    d["t"] = d.t.astype("int64")
    pc = d.c.shift()
    tr = pd.concat([d.h - d.l, (d.h - pc).abs(), (d.l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / ATR_N, adjust=False).mean()
    d["ef"], d["es"] = d.c.ewm(span=FAST, adjust=False).mean(), d.c.ewm(span=SLOW, adjust=False).mean()
    d["short"] = (d.ef < d.es) & (d.c < d.es)
    d["long"] = (d.ef > d.es) & (d.c > d.es)
    return d

def instruments(cl, coins=None):
    out = {}
    for c in (coins or COINS):
        try: it = lst(cl.get_instruments_info(category=CAT, symbol=sym(c)))
        except Exception: it = []
        if it and it[0].get("status") not in (None, "", "Trading"): it = []
        if not it: continue
        lf, pf = it[0].get("lotSizeFilter", {}), it[0].get("priceFilter", {})
        out[c] = {"qtyStep": lf.get("qtyStep") or "0.001", "minQty": f(lf.get("minOrderQty")),
                  "maxMkt": f(lf.get("maxMktOrderQty") or lf.get("maxOrderQty"), 1e18),
                  "minNotional": f(lf.get("minNotionalValue")), "tick": pf.get("tickSize") or "0.0001",
                  "maxLev": f(it[0].get("leverageFilter", {}).get("maxLeverage"), LEV)}
    return out

def q_floor(x, step):
    s = Decimal(str(step)); return (Decimal(str(x)) / s).to_integral_value(ROUND_DOWN) * s

def p_round(x, tick, up):
    t = Decimal(str(tick)); return (Decimal(str(x)) / t).to_integral_value(ROUND_UP if up else ROUND_DOWN) * t

def dstr(x): return format(Decimal(str(x)).normalize(), "f")

def equity_usdt(cl):
    a = lst(cl.get_wallet_balance(accountType="UNIFIED"))
    if not a: raise RuntimeError("пустой ответ get_wallet_balance")
    return f(a[0].get("totalEquity"))

def exch_positions(cl):
    """Все открытые USDT-перпы на бирже: {symbol: {...}} (источник правды)."""
    out, cursor = {}, None
    for _ in range(5):
        kw = dict(category=CAT, settleCoin="USDT", limit=200)
        if cursor: kw["cursor"] = cursor
        r = cl.get_positions(**kw)
        for p in lst(r):
            if f(p.get("size")) > 0 and p.get("side") in ("Buy", "Sell"):
                out[p["symbol"]] = {"dir": "long" if p["side"] == "Buy" else "short", "size": f(p["size"]),
                                    "avg": f(p.get("avgPrice")), "mark": f(p.get("markPrice")),
                                    "upnl": f(p.get("unrealisedPnl")), "sl": f(p.get("stopLoss")),
                                    "value": f(p.get("positionValue")) or f(p["size"]) * f(p.get("markPrice")),
                                    "idx": int(f(p.get("positionIdx"), 0))}
        cursor = (r or {}).get("result", {}).get("nextPageCursor")
        if not cursor: break
    return out

def ticker_info(cl, symbol):
    """Цена маркировки и лучшие цены покупки/продажи (для проверки спреда)."""
    t = lst(cl.get_tickers(category=CAT, symbol=symbol))
    if not t: raise RuntimeError(f"нет тикера {symbol}")
    t = t[0]
    return {"mark": f(t.get("markPrice")) or f(t.get("lastPrice")), "bid": f(t.get("bid1Price")), "ask": f(t.get("ask1Price"))}


def open_order_symbols(cl):
    """Символы с висящими ордерами (без стопов позиций). None — биржа не ответила."""
    try:
        r = lst(cl.get_open_orders(category=CAT, settleCoin="USDT", openOnly=0, limit=50))
        return {o.get("symbol") for o in r if not o.get("stopOrderType") and not o.get("reduceOnly")}
    except Exception:
        return None


def mark_price(cl, symbol):
    t = lst(cl.get_tickers(category=CAT, symbol=symbol))
    if not t: raise RuntimeError(f"нет тикера {symbol}")
    return f(t[0].get("markPrice")) or f(t[0].get("lastPrice"))

def set_lev(cl, symbol, lev):
    try:
        cl.set_leverage(category=CAT, symbol=symbol, buyLeverage=str(lev), sellLeverage=str(lev))
    except Exception as e:
        if err_code(e) != 110043 and "110043" not in str(e): raise   # 110043 = плечо уже такое

def set_stop(cl, symbol, price, tick, direction):
    px = p_round(price, tick, up=(direction == "short"))   # округляем чуть «шире», а не внутрь
    try:
        cl.set_trading_stop(category=CAT, symbol=symbol, positionIdx=0, tpslMode="Full",
                            stopLoss=dstr(px), slTriggerBy="MarkPrice")
    except Exception as e:
        if err_code(e) not in (34040,) and "34040" not in str(e): raise   # 34040 = стоп не изменился
    return float(px)

def set_stop_tp(cl, symbol, stop, tp, tick, direction):
    """Стоп и тейк на бирже одной командой. Округляем «шире», а не внутрь."""
    sp = p_round(stop, tick, up=(direction == "short")); tpp = p_round(tp, tick, up=(direction == "long"))
    try:
        cl.set_trading_stop(category=CAT, symbol=symbol, positionIdx=0, tpslMode="Full",
                            stopLoss=dstr(sp), slTriggerBy="MarkPrice", takeProfit=dstr(tpp), tpTriggerBy="LastPrice")
    except Exception as e:
        if err_code(e) not in (34040,) and "34040" not in str(e): raise
    return float(sp), float(tpp)

def market(cl, symbol, side, qty, reduce_only):
    r = cl.place_order(category=CAT, symbol=symbol, side=side, orderType="Market", qty=dstr(qty),
                       positionIdx=0, reduceOnly=reduce_only)
    return ((r or {}).get("result") or {}).get("orderId", "")

def wait_position(cl, symbol, want_open, tries=6):
    """Ждём, пока позиция появится/исчезнет (демо обновляется с задержкой)."""
    p = None
    for _ in range(tries):
        time.sleep(2)
        try: p = exch_positions(cl).get(symbol)
        except Exception: continue
        if bool(p) == want_open: return p
    return p

def closed_pnl(cl, symbol, since_ms, order_id=None, tries=1):
    """Реализованный PnL (уже за вычетом комиссий) из get_closed_pnl; (цена выхода, pnl) или (None, None)."""
    for i in range(tries):
        if i: time.sleep(2)
        try: rows = lst(cl.get_closed_pnl(category=CAT, symbol=symbol, startTime=int(since_ms) - 60_000, limit=50))
        except Exception: rows = []
        rows = [r for r in rows if f(r.get("updatedTime") or r.get("createdTime")) >= since_ms - 60_000]
        if order_id:
            mine = [r for r in rows if r.get("orderId") == order_id]
            if not mine and i < tries - 1: continue   # запись о закрытии ещё не появилась
            rows = mine or rows
        if rows:
            qty = sum(f(r.get("closedSize")) for r in rows) or 1.0
            px = sum(f(r.get("avgExitPrice")) * f(r.get("closedSize")) for r in rows) / qty
            return px, sum(f(r.get("closedPnl")) for r in rows)
    return None, None


# ---------------- один прогон ----------------
def run_once(cl, inst):
    S = load_state(); gate = news_gate(); errors = []
    global COINS, MAX_POS
    st_cfg = strategy_cfg(); MODE = st_cfg["mode"]; acfg = active_strategy.merged(st_cfg["active"])
    COINS = [c for c in st_cfg["coins"] if c in inst]; MAX_POS = st_cfg["max_positions"]
    COINS += [c for c in S["positions"] if c in inst and c not in COINS]   # монету убрали из списка, а позиция ещё открыта
    new_bar = False
    def safe(label, fn, *a, **kw):
        try: return fn(*a, **kw)
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)[-1]
            msg = f"{label}: {e} (стр. {tb.lineno}, {tb.name})"
            print(f"Ошибка шага: {msg}", file=sys.stderr, flush=True); errors.append(msg); return None

    equity = equity_usdt(cl)
    pos = exch_positions(cl)
    for s_, p in pos.items():
        if p["idx"] != 0:
            log(f"ВНИМАНИЕ: {s_} в режиме хеджирования (positionIdx={p['idx']}). Нужен односторонний режим (One-Way).")
    data = {}
    for c in COINS:
        d = safe(f"{c} свечи", candles, cl, c)
        if d is not None and len(d): data[c] = d

    # защита: аварийный стоп по просадке и фильтр режима рынка (только для НОВЫХ входов)
    reg_cfg, brk_cfg = protection_cfg(); gt_cfg = gate_cfg()
    breaker = circuit_breaker.Breaker(BREAKER_F, brk_cfg, BREAKER_RESET_F)
    try:
        brk = breaker.update(equity)
    except Exception as e:
        brk = {"block_new": True, "close_all": False, "messages": [f"аварийный стоп: ошибка ({str(e)[:100]}), новые входы в этом прогоне пропускаю"],
               "dd_day": 0.0, "dd_peak": 0.0, "peak": None}
    for m in brk["messages"]: log(m)
    log(breaker.status_line(brk))
    reg = regime_filter.evaluate(cl, data, reg_cfg, log=log)
    log(reg["text"])
    act = {}
    if MODE == "active":
        for c_, d_ in data.items():
            x = safe(f"{c_} сигналы", active_strategy.compute, d_[["t", "o", "h", "l", "c"]], acfg, None, BAR_MS)
            if x is not None: act[c_] = x
        log(f"стратегия: АКТИВНАЯ (пробой {acfg['breakout_n']} свечей 4h по тренду, стоп {acfg['sl_atr']:g} ATR, "
            f"тейк {acfg['tp_atr']:g} ATR, до {acfg['max_hold_bars']} свечей), риск {st_cfg['risk_pct']:g}%"
            if acfg["kind"] == "breakout" else f"стратегия: АКТИВНАЯ ({acfg['kind']}), риск {st_cfg['risk_pct']:g}%")
    else:
        log("стратегия: пересечение EMA50/200 на 4h (старый режим ema_cross)")

    def jbase(coin, direction=""):
        r = {"coin": coin, "symbol": sym(coin), "direction": direction, "equity": equity}
        d = data.get(coin)
        if d is not None:
            b = d.iloc[-1]; r.update(bar_time=bar_iso(b.t), close=b.c, ema50=b.ef, ema200=b.es, atr=b.atr)
        return r

    def finish(coin, p, px, pnl, reason, oid=""):
        if pnl is None:   # нет данных о закрытии: оценка без комиссий
            sgn = 1 if p["dir"] == "long" else -1; px = px or p["stop"]; pnl = sgn * (px - p["entry"]) * p["qty"]
            reason += " (PnL оценочный)"
        row = jbase(coin, p["dir"]); row.update(action="exit", reason=reason, price=px, qty=p["qty"], stop=p["stop"],
              pnl_usd=pnl, pnl_pct_equity=100 * pnl / max(p.get("equity_at_entry", equity), 1e-9),
              outcome="win" if pnl > 0 else "loss", order_id=oid)
        if pnl < 0 and "на бирже" in reason:   # стоп-аут -> пауза перед повторным входом по монете
            S.setdefault("stopouts", {})[coin] = int(time.time() * 1000)
        journal(row); S["positions"].pop(coin, None); save_state(S)
        log(f"{coin}: закрыт {'лонг' if p['dir']=='long' else 'шорт'} по {px:.6g}, PnL {pnl:+.2f} USDT ({reason})")

    # 1) сверка с биржей: позиция исчезла -> сработал стоп (или закрыли вручную)
    for coin, p in list(S["positions"].items()):
        ep = pos.get(sym(coin))
        if ep is None:   # перепроверяем: демо иногда отдаёт пустой список
            ep = wait_position(cl, sym(coin), want_open=True, tries=2)
            if ep: pos[sym(coin)] = ep
        if ep is None or ep["dir"] != p["dir"]:
            px, pnl = closed_pnl(cl, sym(coin), p["opened_ms"])
            finish(coin, p, px, pnl, "позиция закрыта на бирже (стоп, тейк или вручную)")
        else:
            p["qty"] = ep["size"]   # размер берём с биржи
    # позиции бота на бирже, которых нет в файле (файл потерян) -> подхватываем
    for coin in COINS:
        ep = pos.get(sym(coin))
        if ep and coin not in S["positions"] and coin in data and ep["idx"] == 0:
            d = data[coin]; dist = STOP_K * float(d.atr.iloc[-1])
            stop = ep["sl"] or (ep["avg"] - dist if ep["dir"] == "long" else ep["avg"] + dist)
            S["positions"][coin] = {"dir": ep["dir"], "qty": ep["size"], "entry": ep["avg"], "best": float(d.c.iloc[-1]),
                                    "stop": stop, "opened_ms": int(time.time() * 1000), "equity_at_entry": equity,
                                    "opened_at": now_utc().isoformat(), "adopted": True}
            log(f"{coin}: найдена позиция на бирже без записи, беру под управление (стоп {stop:.6g})")
            if not ep["sl"] and coin in inst:
                S["positions"][coin]["stop"] = safe(f"{coin} стоп", set_stop, cl, sym(coin), stop, inst[coin]["tick"], ep["dir"]) or stop
    save_state(S)

    # 1б) «закрыть всё» (только если включено в config.yaml, по умолчанию выключено)
    if brk.get("close_all"):
        for coin, p in list(S["positions"].items()):
            if coin not in inst: continue
            try:
                oid = market(cl, sym(coin), "Buy" if p["dir"] == "short" else "Sell", q_floor(p["qty"], inst[coin]["qtyStep"]), True)
                wait_position(cl, sym(coin), want_open=False)
                px, pnl = closed_pnl(cl, sym(coin), p["opened_ms"], oid, tries=4)
                finish(coin, p, px or mark_price(cl, sym(coin)), pnl, "аварийный стоп: закрыть всё при большой просадке", oid)
            except Exception as e:
                msg = f"{coin}: аварийное закрытие не удалось: {e}"
                print(f"Ошибка шага: {msg}", file=sys.stderr, flush=True); errors.append(msg)
        if not any(c in S["positions"] for c in inst): breaker.mark_closed_all()

    # 2) по каждой монете: выход по смене сигнала, подтяжка стопа, свежий вход
    pos = exch_positions(cl)
    notional = sum(p["value"] for p in pos.values())
    def try_enter(coin, fresh, why="signal"):
        """Открыть позицию по монете в направлении fresh. why: signal | daily (сделка дня). True = открыта."""
        nonlocal notional
        d = data[coin]; b = d.iloc[-1]; ins = inst[coin]; last_t = int(d.t.iloc[-1])
        row = jbase(coin, fresh)
        if MODE == "active":
            atr_sig = float(act[coin].atr.iloc[-1]); dist = acfg["sl_atr"] * atr_sig; risk = st_cfg["risk_pct"] / 100
        else:
            dist = STOP_K * float(b.atr); risk = RISK
        if coin in S["positions"] or sym(coin) in pos:
            row.update(action="skip", reason="уже есть позиция по монете"); journal(row); return False
        if gate.get("block_new_entries") or gate["size_mult"] <= 0:
            row.update(action="skip", reason="новостной фильтр запретил вход"); journal(row); return False
        if brk["block_new"]:
            row.update(action="skip", reason="аварийный стоп по просадке запретил вход"); journal(row)
            log(f"{coin}: свежий сигнал {fresh}, но аварийный стоп запрещает новые входы — пропуск"); return False
        rmult = regime_filter.size_mult(reg["score"], fresh, reg_cfg) if reg.get("enabled") else 1.0
        if rmult <= 0:
            row.update(action="skip", reason=f"фильтр режима рынка ({reg['score']:.0f}/100) запретил {fresh}"); journal(row)
            log(f"{coin}: свежий сигнал {fresh}, но режим рынка {reg['score']:.0f}/100 против него — пропуск"); return False
        if len(pos) >= MAX_POS:
            row.update(action="skip", reason=f"уже {MAX_POS} позиции"); journal(row)
            log(f"{coin}: свежий сигнал {fresh}, но уже {MAX_POS} позиции — пропуск"); return False
        tk = ticker_info(cl, sym(coin)); px = tk["mark"]
        lev = int(min(LEV, ins["maxLev"]))
        if rmult < 1: log(f"{coin}: режим рынка {reg['score']:.0f}/100 против {fresh} — вход половинным размером")
        qty = min(risk * equity * gate["size_mult"] * rmult / dist, lev * equity / px,
                  max(0.0, TOTAL_NOTIONAL_X * equity - notional) / px, ins["maxMkt"])
        qty = q_floor(qty, ins["qtyStep"])
        if float(qty) < ins["minQty"] or float(qty) * px < ins["minNotional"] or qty <= 0:
            row.update(action="skip", reason=f"объём {qty} меньше минимального / лимит объёма"); journal(row); return False
        # проверка перед сделкой: план стопа/тейка от текущей цены
        if MODE == "active":
            p_stop, p_tp = active_strategy.levels(px, atr_sig, fresh, acfg)
        else:
            p_stop, p_tp = (px + dist if fresh == "short" else px - dist), None
        ok, bad, notes = pre_trade_gate.check(
            {"coin": coin, "direction": fresh, "price": px, "qty": float(qty), "stop": p_stop, "tp": p_tp,
             "equity": equity, "risk_pct": 100 * risk, "require_tp": MODE == "active"},
            {"profile": st_cfg.get("profile"), "positions": S["positions"], "exch_symbols": set(pos), "symbol": sym(coin),
             "open_order_symbols": open_order_symbols(cl), "news_block": bool(gate.get("block_new_entries")),
             "stopouts": S.get("stopouts", {}), "now_ms": time.time() * 1000, "bid": tk["bid"], "ask": tk["ask"]},
            gt_cfg)
        for n_ in notes: log(f"{coin}: {n_}")
        if not ok:
            row.update(action="skip", reason="проверка перед сделкой: " + "; ".join(bad), price=px, qty=float(qty), stop=p_stop)
            journal(row)
            log(f"{coin}: {('сделка дня ' if why == 'daily' else 'сигнал ')}{'лонг' if fresh=='long' else 'шорт'} НЕ открыт — проверка перед сделкой: " + "; ".join(bad))
            return False
        set_lev(cl, sym(coin), lev)
        oid = market(cl, sym(coin), "Sell" if fresh == "short" else "Buy", qty, False)
        ep = wait_position(cl, sym(coin), want_open=True)
        fill = ep["avg"] if ep and ep["avg"] > 0 else px
        real_qty = ep["size"] if ep else float(qty)
        stop = fill + dist if fresh == "short" else fill - dist
        S["positions"][coin] = {"dir": fresh, "qty": real_qty, "entry": fill, "best": fill, "stop": float(stop),
                                "opened_ms": int(time.time() * 1000) - 30_000, "opened_at": now_utc().isoformat(),
                                "equity_at_entry": equity, "signal_bar": bar_iso(b.t), "order_id": oid}
        save_state(S)
        if MODE == "active":
            stop, tp = active_strategy.levels(fill, atr_sig, fresh, acfg)
            sp, tpp = set_stop_tp(cl, sym(coin), stop, tp, ins["tick"], fresh)
            S["positions"][coin].update(stop=sp, tp=tpp, mode="active", entry_bar_t=last_t + BAR_MS,
                                        max_hold=int(acfg["max_hold_bars"]))
            save_state(S)
        else:
            S["positions"][coin]["stop"] = set_stop(cl, sym(coin), stop, ins["tick"], fresh); save_state(S)
        notional += real_qty * fill; pos[sym(coin)] = {"value": real_qty * fill}
        row.update(action="entry", reason=((f"сделка дня: самый сильный сетап по тренду {fresh}" if why == "daily" else f"пробой/сигнал активной стратегии {fresh}") if MODE == "active" else f"свежий сигнал EMA{FAST}/{SLOW} {fresh}") + (f", размер x{rmult:g} (режим рынка)" if rmult < 1 else ""), price=fill, qty=real_qty,
                   notional=real_qty * fill, stop=S["positions"][coin]["stop"], order_id=oid); journal(row)
        tp_txt = f", тейк {S['positions'][coin]['tp']:.6g}" if S["positions"][coin].get("tp") else ""
        log(f"{coin}: ОТКРЫТ {'ЛОНГ' if fresh=='long' else 'ШОРТ'} {real_qty} по {fill:.6g}, стоп {S['positions'][coin]['stop']:.6g}{tp_txt}, "
            f"объём {real_qty*fill:.0f} USDT")
        S["last_entry_ms"] = int(time.time() * 1000); save_state(S)
        return True

    for coin in COINS:
        d = data.get(coin)
        if d is None or coin not in inst: continue
        try:
            last_t = int(d.t.iloc[-1]); prev = S["last_bar"].get(coin)
            if prev is not None and last_t <= prev: continue        # новой закрытой 4h свечи нет
            new_bar = True
            new_idx = (list(d.index[d.t > prev]) if prev else [d.index[-1]])[-2:]
            b = d.iloc[-1]; p = S["positions"].get(coin); ins = inst[coin]
            if p and p.get("mode") == "active":
                held = int((last_t - int(p.get("entry_bar_t", last_t))) // BAR_MS) + 1
                if held >= int(p.get("max_hold", acfg["max_hold_bars"])):
                    oid = market(cl, sym(coin), "Buy" if p["dir"] == "short" else "Sell", q_floor(p["qty"], ins["qtyStep"]), True)
                    wait_position(cl, sym(coin), want_open=False)
                    px, pnl = closed_pnl(cl, sym(coin), p["opened_ms"], oid, tries=4)
                    finish(coin, p, px or float(b.c), pnl, f"время вышло ({held} свечей 4h)", oid)
                    pos.pop(sym(coin), None)
                    notional = sum(x["value"] for x in pos.values())
            elif p:
                if not bool(b[p["dir"]]):
                    oid = market(cl, sym(coin), "Buy" if p["dir"] == "short" else "Sell", q_floor(p["qty"], ins["qtyStep"]), True)
                    wait_position(cl, sym(coin), want_open=False)
                    px, pnl = closed_pnl(cl, sym(coin), p["opened_ms"], oid, tries=4)
                    finish(coin, p, px or float(b.c), pnl, "смена сигнала (условие EMA закончилось на 4h свече)", oid)
                    pos.pop(sym(coin), None)
                    notional = sum(x["value"] for x in pos.values())
                else:
                    new = p["stop"]
                    for i in new_idx:
                        bb = d.loc[i]
                        if p["dir"] == "short": p["best"] = min(p["best"], float(bb.c)); new = min(new, p["best"] + STOP_K * float(bb.atr))
                        else: p["best"] = max(p["best"], float(bb.c)); new = max(new, p["best"] - STOP_K * float(bb.atr))
                    tighter = (new < p["stop"]) if p["dir"] == "short" else (new > p["stop"])
                    if tighter:
                        mk = mark_price(cl, sym(coin))
                        if (p["dir"] == "long" and new >= mk) or (p["dir"] == "short" and new <= mk):
                            # новый стоп уже за ценой: биржа его не примет -> закрываем по рынку
                            oid = market(cl, sym(coin), "Buy" if p["dir"] == "short" else "Sell", q_floor(p["qty"], ins["qtyStep"]), True)
                            wait_position(cl, sym(coin), want_open=False)
                            px, pnl = closed_pnl(cl, sym(coin), p["opened_ms"], oid, tries=4)
                            finish(coin, p, px or mk, pnl, f"цена уже за новым стопом {new:.6g}", oid)
                            pos.pop(sym(coin), None)
                        else:
                            old = p["stop"]; p["stop"] = set_stop(cl, sym(coin), new, ins["tick"], p["dir"]); save_state(S)
                            row = jbase(coin, p["dir"]); row.update(action="stop_move", reason=f"трейлинг 5xATR {old:.6g} -> {p['stop']:.6g}",
                                       price=float(b.c), qty=p["qty"], stop=p["stop"]); journal(row)
                            log(f"{coin}: стоп подтянут {old:.6g} -> {p['stop']:.6g}")
            # свежий сигнал?
            fresh = None
            if MODE == "active":
                x = act.get(coin)
                if x is not None and int(x.t.iloc[-1]) == last_t:      # сигнал только на последней закрытой свече
                    fresh = "long" if bool(x.long_sig.iloc[-1]) else "short" if bool(x.short_sig.iloc[-1]) else None
            else:
                for i in new_idx:
                    if i == 0: continue
                    for dr in ("short", "long"):
                        if bool(d.loc[i, dr]) and not bool(d.loc[i - 1, dr]) and bool(b[dr]): fresh = dr
            S["last_bar"][coin] = last_t; save_state(S)
            if not fresh: continue
            try_enter(coin, fresh)
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)[-1]
            msg = f"{coin}: {e} (стр. {tb.lineno}, {tb.name})"
            print(f"Ошибка шага: {msg}", file=sys.stderr, flush=True); errors.append(msg)
            if "10001" in str(e) and "position idx" in str(e).lower():
                print("Подсказка: переключите фьючерсы USDT в односторонний режим (One-Way) в настройках Bybit.", file=sys.stderr)

    # 2б) «сделка дня»: если за 24 часа не было ни одного входа — открыть самый сильный сетап по тренду
    if MODE == "active" and st_cfg["daily_min_trade"] and new_bar and not brk["block_new"]:
        last_e = int(S.get("last_entry_ms", 0) or 0)
        if time.time() * 1000 - last_e >= 86_400_000 and len(pos) < MAX_POS:
            best = None; cands = []
            for coin, x in act.items():
                if coin not in inst or coin in S["positions"] or sym(coin) in pos: continue
                tr_, atr_ = int(x.trend.iloc[-1]), float(x.atr.iloc[-1])
                hh, ll = float(x.h.iloc[-31:-1].max()), float(x.l.iloc[-31:-1].min())
                if tr_ == 0 or not atr_ > 0 or len(x) < 31: continue
                sc_ = (float(x.c.iloc[-1]) - hh) / atr_ if tr_ > 0 else (ll - float(x.c.iloc[-1])) / atr_
                cands.append((sc_, coin, "long" if tr_ > 0 else "short"))
                if best is None or sc_ > best[0]: best = (sc_, coin, "long" if tr_ > 0 else "short")
            if best:
                log(f"сделка дня: за 24 часа входов не было — беру самый сильный сетап {best[1]} "
                    f"{'ЛОНГ' if best[2]=='long' else 'ШОРТ'} (до уровня пробоя {-best[0]:.2f} ATR)" if best[0] < 0 else
                    f"сделка дня: {best[1]} {'ЛОНГ' if best[2]=='long' else 'ШОРТ'}")
                for k_, (sc_, c_, d_) in enumerate(sorted(cands, reverse=True)[:3]):
                    if k_: log(f"сделка дня: следующий кандидат {c_} {'ЛОНГ' if d_=='long' else 'ШОРТ'}")
                    try:
                        if try_enter(c_, d_, "daily"): break
                    except Exception as e:
                        msg = f"сделка дня {c_}: {e}"; print(f"Ошибка шага: {msg}", file=sys.stderr, flush=True); errors.append(msg); break
            else:
                log("сделка дня: нет монеты с ясным трендом, пропускаю")

    # 3) итог
    save_state(S)
    try: equity = equity_usdt(cl); pos = exch_positions(cl)
    except Exception: pass
    sig = {c: ("лонг" if bool(d.long.iloc[-1]) else "шорт" if bool(d.short.iloc[-1]) else "нет") for c, d in data.items()}
    log(f"капитал {equity:.2f} USDT, позиций {len([c for c in COINS if sym(c) in pos])}/{MAX_POS}, ошибок {len(errors)}")
    log("тренд 4h: " + ", ".join(f"{c} {v}" for c, v in sig.items()))
    for coin, p in S["positions"].items():
        ep = pos.get(sym(coin), {})
        log(f"  {coin} {'лонг' if p['dir']=='long' else 'шорт'} {p['qty']} вход {p['entry']:.6g} стоп {p['stop']:.6g} "
            f"PnL {f(ep.get('upnl')):+.2f} USDT")


# ---------------- почасовой отчёт по балансу ----------------
def _report_history():
    rows = []
    try:
        with open(REPORT_F, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                try: rows.append((datetime.fromisoformat(r["time"]), f(r.get("equity"))))
                except Exception: continue
    except FileNotFoundError: pass
    except Exception as e: print(f"Не смог прочитать {REPORT_F}: {e}", file=sys.stderr)
    return [r for r in rows if r[1] > 0]

def _chg(now_eq, old_eq):
    d = now_eq - old_eq
    return d, (100 * d / old_eq if old_eq else 0.0)

def balance_report(cl):
    """Печатает отчёт по балансу демо-счёта и дописывает строку в отчёт.csv. Никогда не роняет бота."""
    try:
        acc = lst(cl.get_wallet_balance(accountType="UNIFIED"))
        if not acc: raise RuntimeError("пустой ответ get_wallet_balance")
        a = acc[0]
        equity = f(a.get("totalEquity"))
        coins = {c.get("coin"): c for c in (a.get("coin") or [])}
        u = coins.get("USDT", {}); b = coins.get("BTC", {})
        usdt = f(u.get("walletBalance")) or f(u.get("equity"))
        btc_qty = f(b.get("walletBalance")) or f(b.get("equity"))
        btc_val = f(b.get("usdValue"))
        upnl_wallet = f(a.get("totalPerpUPL"))
    except Exception as e:
        print(f"{TAG} Отчёт по балансу: не удалось получить баланс ({str(e)[:150]}). Попробую в следующий час.",
              file=sys.stderr, flush=True)
        return
    try: pos = exch_positions(cl)
    except Exception as e:
        pos = None
        print(f"{TAG} Отчёт: не удалось получить позиции ({str(e)[:120]})", file=sys.stderr, flush=True)
    upnl = sum(p["upnl"] for p in pos.values()) if pos else upnl_wallet
    now = datetime.now().replace(microsecond=0)
    hist = _report_history()
    ch24 = None
    if hist:
        target = now - timedelta(hours=24)
        old = [r for r in hist if r[0] <= now - timedelta(hours=20)]   # запись примерно сутки назад
        if old:
            ch24 = _chg(equity, min(old, key=lambda r: abs((r[0] - target).total_seconds()))[1])
    first = _chg(equity, hist[0][1]) if hist else None

    line = "=" * 60
    print(line, flush=True)
    log("ОТЧЁТ ПО БАЛАНСУ (демо-счёт)")
    print(f"  Всего на счёте:          {equity:,.2f} USD".replace(",", " "))
    if ch24: print(f"  За 24 часа:              {ch24[0]:+,.2f} USD ({ch24[1]:+.2f}%)".replace(",", " "))
    else:    print("  За 24 часа:              пока нет данных (нужна запись суточной давности)")
    if first: print(f"  С первого отчёта ({hist[0][0]:%d.%m %H:%M}): {first[0]:+,.2f} USD ({first[1]:+.2f}%)".replace(",", " "))
    else:     print("  С первого отчёта:        это первый отчёт, от него и будем считать")
    print(f"  USDT:                    {usdt:,.2f}".replace(",", " "))
    print(f"  BTC (спот-бот):          {btc_qty:.6f} BTC = {btc_val:,.2f} USD".replace(",", " "))
    print(f"  Нереализованный PnL:     {upnl:+,.2f} USD".replace(",", " "))
    if pos is None:
        print("  Открытые позиции:        не удалось получить")
    elif not pos:
        print("  Открытые позиции:        нет")
    else:
        print(f"  Открытые позиции ({len(pos)}):")
        for s_, p in sorted(pos.items()):
            print(f"    {coin_of(s_):<5} {'ЛОНГ' if p['dir']=='long' else 'ШОРТ'}  размер {p['size']:g}  "
                  f"вход {p['avg']:.6g}  сейчас {p['mark']:.6g}  PnL {p['upnl']:+.2f} USD")
    print(line, flush=True)

    try:
        new = not os.path.exists(REPORT_F) or os.path.getsize(REPORT_F) == 0
        with open(REPORT_F, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as fh:
            w = csv.writer(fh, delimiter=";")   # ; — так русский Excel сразу раскладывает по колонкам
            if new: w.writerow(REPORT_COLS)
            w.writerow([now.isoformat(sep=" ", timespec="minutes"), f"{equity:.2f}", f"{usdt:.2f}", f"{upnl:.2f}",
                        "" if pos is None else len(pos), "" if not ch24 else f"{ch24[0]:.2f}", f"{btc_val:.2f}"])
    except PermissionError:
        print(f"{TAG} Не могу записать {REPORT_F}: файл открыт в Excel. Закройте его — запишу в следующий час.",
              file=sys.stderr, flush=True)
    except Exception as e:
        print(f"{TAG} Не могу записать {REPORT_F}: {e}", file=sys.stderr, flush=True)


def try_one_way(cl):
    try: cl.switch_position_mode(category=CAT, coin="USDT", mode=0)
    except Exception: pass   # уже one-way (110025) или есть позиции — не важно


def sleep_to_next_run():
    n = datetime.now(); nxt = n.replace(minute=5, second=0, microsecond=0)
    if nxt <= n: nxt += timedelta(hours=1)
    time.sleep(max(30, (nxt - n).total_seconds()))


def main():
    ap = argparse.ArgumentParser(description="Фьючерсный бот Bybit, ТОЛЬКО демо-счёт")
    ap.add_argument("--once", action="store_true"); ap.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(STATE_F), exist_ok=True); os.makedirs(os.path.dirname(JOURNAL_F), exist_ok=True)
    cl = make_client()
    sc = strategy_cfg()
    log(f"демо-счёт Bybit ({cl.endpoint}), профиль {'АГРЕССИВНЫЙ' if sc['profile']=='aggressive' else 'обычный'}, "
        f"монет {len(sc['coins'])}: {' '.join(sc['coins'])}")
    log(f"стратегия {'АКТИВНАЯ' if sc['mode']=='active' else 'EMA50/200'}, риск {sc['risk_pct'] if sc['mode']=='active' else RISK*100:g}% на сделку, "
        f"плечо до {LEV}x, макс. {sc['max_positions']} позиций" + (", сделка каждый день: да" if sc["daily_min_trade"] else ""))
    try_one_way(cl)
    inst = None; missing = set()
    while True:
        try:
            bybit_time.ensure_synced()
            want = set(strategy_cfg()["coins"])
            if not inst or not want <= set(inst) | missing:
                inst = instruments(cl, sorted(want))
                missing = want - set(inst)
                if missing: log(f"не найдены на Bybit (пропускаю): {', '.join(sorted(missing))}")
            run_once(cl, inst)
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            print(f"Ошибка шага: {e}\n  " + " <- ".join(f"стр.{t.lineno} {t.name}" for t in tb[-3:]), file=sys.stderr, flush=True)
            if "10002" in str(e):
                bybit_time.on_10002()
            if "403" in str(e) or "10024" in str(e):
                print("Похоже, Bybit блокирует доступ (страна/IP) или неверный ключ.", file=sys.stderr)
            if "10003" in str(e) or "10004" in str(e) or "10005" in str(e):
                print("Проблема с ключом: нужен ключ, созданный в режиме Демо-торговли, с правами на Контракты (Деривативы).",
                      file=sys.stderr)
            if a.once and not a.loop:
                balance_report(cl); sys.exit(1)
        balance_report(cl)
        if a.once and not a.loop: break
        log("следующая проверка в ~:05 следующего часа")
        sleep_to_next_run()


if __name__ == "__main__":
    main()
