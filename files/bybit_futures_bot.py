#!/usr/bin/env python3
"""Фьючерсный бот для ДЕМО-счёта Bybit (Demo Trading, api-demo.bybit.com). ТОЛЬКО ДЕМО, режима live НЕТ.

Стратегия (как kraken_futures_bot.py / alts_short_backtest.py "EMA (50,200)", лонг и шорт):
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

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(BASE, "state", "bybit_futures_state.json")
NEWS_F = os.path.join(BASE, "state", "news_gate.json")   # необязательный файл; нет файла = торговать как обычно
JOURNAL_F = os.path.join(BASE, "journal", "bybit_futures_journal.csv")
REPORT_F = os.path.join(BASE, "отчёт.csv")   # история баланса, открывается в Excel
REPORT_COLS = ["time", "equity", "usdt", "unrealized_pnl", "open_positions", "change_24h", "btc_value"]
COINS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK"]
FAST, SLOW, ATR_N, STOP_K = 50, 200, 14, 5.0
RISK, LEV, MAX_POS, TOTAL_NOTIONAL_X = 0.02, 3, 4, 3.0
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
    c = HTTP(testnet=False, demo=True, tld="com", api_key=key, api_secret=secret, recv_window=10000)
    # жёсткая защита: работаем только с демо-сервером
    if not getattr(c, "demo", False) or "api-demo." not in str(getattr(c, "endpoint", "")):
        sys.exit(f"ОТКАЗ: клиент не в демо-режиме ({getattr(c, 'endpoint', '?')}). Этот бот работает только на демо-счёте.")
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

def instruments(cl):
    out = {}
    for c in COINS:
        it = lst(cl.get_instruments_info(category=CAT, symbol=sym(c)))
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
            finish(coin, p, px, pnl, "позиция закрыта на бирже (стоп-лосс или вручную)")
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

    # 2) по каждой монете: выход по смене сигнала, подтяжка стопа, свежий вход
    pos = exch_positions(cl)
    notional = sum(p["value"] for p in pos.values())
    for coin in COINS:
        d = data.get(coin)
        if d is None or coin not in inst: continue
        try:
            last_t = int(d.t.iloc[-1]); prev = S["last_bar"].get(coin)
            if prev is not None and last_t <= prev: continue        # новой закрытой 4h свечи нет
            new_idx = (list(d.index[d.t > prev]) if prev else [d.index[-1]])[-2:]
            b = d.iloc[-1]; p = S["positions"].get(coin); ins = inst[coin]
            if p:
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
            for i in new_idx:
                if i == 0: continue
                for dr in ("short", "long"):
                    if bool(d.loc[i, dr]) and not bool(d.loc[i - 1, dr]) and bool(b[dr]): fresh = dr
            S["last_bar"][coin] = last_t; save_state(S)
            if not fresh: continue
            row = jbase(coin, fresh); dist = STOP_K * float(b.atr)
            if coin in S["positions"] or sym(coin) in pos:
                row.update(action="skip", reason="уже есть позиция по монете"); journal(row); continue
            if gate.get("block_new_entries") or gate["size_mult"] <= 0:
                row.update(action="skip", reason="новостной фильтр запретил вход"); journal(row); continue
            if len(pos) >= MAX_POS:
                row.update(action="skip", reason=f"уже {MAX_POS} позиции"); journal(row)
                log(f"{coin}: свежий сигнал {fresh}, но уже {MAX_POS} позиции — пропуск"); continue
            px = mark_price(cl, sym(coin))
            lev = int(min(LEV, ins["maxLev"]))
            qty = min(RISK * equity * gate["size_mult"] / dist, lev * equity / px,
                      max(0.0, TOTAL_NOTIONAL_X * equity - notional) / px, ins["maxMkt"])
            qty = q_floor(qty, ins["qtyStep"])
            if float(qty) < ins["minQty"] or float(qty) * px < ins["minNotional"] or qty <= 0:
                row.update(action="skip", reason=f"объём {qty} меньше минимального / лимит объёма"); journal(row); continue
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
            S["positions"][coin]["stop"] = set_stop(cl, sym(coin), stop, ins["tick"], fresh); save_state(S)
            notional += real_qty * fill; pos[sym(coin)] = {"value": real_qty * fill}
            row.update(action="entry", reason=f"свежий сигнал EMA{FAST}/{SLOW} {fresh}", price=fill, qty=real_qty,
                       notional=real_qty * fill, stop=S["positions"][coin]["stop"], order_id=oid); journal(row)
            log(f"{coin}: ОТКРЫТ {'ЛОНГ' if fresh=='long' else 'ШОРТ'} {real_qty} по {fill:.6g}, стоп {S['positions'][coin]['stop']:.6g}, "
                f"объём {real_qty*fill:.0f} USDT")
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)[-1]
            msg = f"{coin}: {e} (стр. {tb.lineno}, {tb.name})"
            print(f"Ошибка шага: {msg}", file=sys.stderr, flush=True); errors.append(msg)
            if "10001" in str(e) and "position idx" in str(e).lower():
                print("Подсказка: переключите фьючерсы USDT в односторонний режим (One-Way) в настройках Bybit.", file=sys.stderr)

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
    log(f"демо-счёт Bybit ({cl.endpoint}), монеты: {' '.join(COINS)}, риск {RISK*100:.0f}%, плечо до {LEV}x, макс. {MAX_POS} позиции")
    try_one_way(cl)
    inst = None
    while True:
        try:
            if not inst: inst = instruments(cl)
            run_once(cl, inst)
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            print(f"Ошибка шага: {e}\n  " + " <- ".join(f"стр.{t.lineno} {t.name}" for t in tb[-3:]), file=sys.stderr, flush=True)
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
