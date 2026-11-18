#!/usr/bin/env python3
"""DCA-бот для Bybit, спот BTC/USDT. Режимы: backtest | testnet | live (выключен по умолчанию).

  python bot.py fetch                   # скачать историю свечей
  python bot.py backtest [--tf 1d|4h]   # бэктест + график backtest.png
  python bot.py testnet --once          # один шаг на тестовой бирже Bybit (нужны ключи testnet)
  python bot.py testnet --loop          # работать постоянно (проверка раз в час)
  python bot.py live --once --i-understand-real-money   # РЕАЛЬНЫЕ деньги, только если live_enabled: true
"""
import argparse, dataclasses, json, math, os, sys, time
import yaml
from strategy import State, decide

HERE = os.path.dirname(os.path.abspath(__file__))


def load_cfg(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if cfg.get("category") != "spot":
        sys.exit("ОШИБКА: разрешён только спот (category: spot). Фьючерсы и плечо запрещены.")
    if cfg.get("symbol") != "BTCUSDT":
        sys.exit("ОШИБКА: бот рассчитан только на BTCUSDT.")
    lim = cfg["limits"]
    if lim["max_order_usdt"] > lim["max_total_budget_usdt"]:
        sys.exit("ОШИБКА: max_order_usdt не может быть больше max_total_budget_usdt.")
    return cfg


# ---------------------------------------------------------------- backtest
def cmd_backtest(cfg, a):
    from backtest import run
    import pandas as pd
    if not os.path.exists(os.path.join(HERE, f"data/BTCUSDT_{a.tf}.csv")):
        sys.exit("Нет данных. Сначала: python bot.py fetch")
    rows, df = run(cfg, a.tf, a.chart)
    t = pd.DataFrame(rows).set_index("strategy")
    pd.set_option("display.width", 200); pd.set_option("display.max_columns", 20)
    meta = json.load(open(os.path.join(HERE, "data/meta.json"))).get(a.tf, {})
    print(f"Данные: {meta.get('source')} | свечи {a.tf} | {df.time.iloc[0]:%Y-%m-%d} — {df.time.iloc[-1]:%Y-%m-%d} "
          f"| BTC: {df.close.iloc[0]:.2f} -> {df.close.iloc[-1]:.2f}")
    print(t.to_string())
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    t.to_csv(os.path.join(HERE, f"results/backtest_{a.tf}.csv"))
    print(f"\nГрафик: {os.path.abspath(a.chart)}")


# ---------------------------------------------------------------- testnet / live
def make_client(testnet):
    import keys; key, secret = keys.load()
    if not key or not secret:
        where = "https://www.bybit.com (Демо-торговля)" if testnet else "https://www.bybit.com"
        sys.exit("Нет API-ключей. Создайте ключ на " + where + " (API Management, права: только Spot Trade, "
                 "БЕЗ вывода средств) и задайте переменные окружения:\n"
                 "  export BYBIT_API_KEY=...\n  export BYBIT_API_SECRET=...")
    from pybit.unified_trading import HTTP
    tld = os.environ.get("BYBIT_TLD", "com")  # com = глобальный Bybit, eu = Bybit EU
    # testnet-режим = Демо-торговля Bybit (api-demo.bybit.com), виртуальные деньги
    return HTTP(testnet=False, demo=testnet, api_key=key, api_secret=secret, tld=tld, recv_window=10000)


def load_state(path):
    """Возвращает (State, есть_ли_сохранённая_история). Пустой/битый файл = истории нет."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        known = {f.name for f in dataclasses.fields(State)}
        st = State(**{k: v for k, v in d.items() if k in known})
        return st, (st.trades > 0 or st.spent_usdt > 0 or st.synced_from_exchange)
    except FileNotFoundError:
        return State(), False
    except Exception as e:
        print(f"Файл состояния {path} повреждён ({e}), восстановлю историю с биржи.")
        return State(), False


def save_state(st, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dataclasses.asdict(st), fh, indent=2)
    os.replace(tmp, path)  # атомарно: окно можно закрыть в любой момент


BOT_TAG = "cbdca"   # метка orderLinkId у ордеров этого бота


def link_id(reason):
    r = "".join(ch for ch in reason if ch.isalnum())[:10]
    return f"{BOT_TAG}-{r}-{int(time.time() * 1000)}"


def fetch_spot_buys(client, sym, days=90):
    """Все исполненные покупки/продажи BTCUSDT на споте за `days` дней, сгруппированные по ордерам.
    Сначала история исполнений (get_executions), при ошибке — история ордеров. Bybit отдаёт максимум 7 дней за запрос."""
    end = int(time.time() * 1000)
    start_all = end - days * 86400_000
    week = 7 * 86400_000 - 1000

    def windows():
        e = end
        while e > start_all:
            s = max(start_all, e - week)
            yield s, e
            e = s - 1

    def paged(fn, s, e):
        cursor = None
        for _ in range(50):
            kw = dict(category="spot", symbol=sym, startTime=s, endTime=e, limit=100)
            if cursor:
                kw["cursor"] = cursor
            r = fn(**kw)
            res = (r or {}).get("result", {}) or {}
            for x in res.get("list", []) or []:
                yield x
            cursor = res.get("nextPageCursor")
            if not cursor:
                break

    orders = {}
    try:
        for s, e in windows():
            for x in paged(client.get_executions, s, e):
                oid = x.get("orderId") or x.get("execId")
                o = orders.setdefault(oid, {"side": x.get("side"), "qty": 0.0, "value": 0.0, "fee": 0.0,
                                            "fee_ccy": x.get("feeCurrency") or "", "ts": 0.0,
                                            "link": x.get("orderLinkId") or ""})
                qty = float(x.get("execQty") or 0)
                val = float(x.get("execValue") or 0) or qty * float(x.get("execPrice") or 0)
                o["qty"] += qty; o["value"] += val; o["fee"] += float(x.get("execFee") or 0)
                o["ts"] = max(o["ts"], float(x.get("execTime") or 0) / 1000)
        src = "история исполнений"
    except Exception as e1:
        print(f"  get_executions не сработал ({str(e1)[:120]}), беру историю ордеров")
        orders = {}
        for s, e in windows():
            for x in paged(client.get_order_history, s, e):
                qty = float(x.get("cumExecQty") or 0)
                if qty <= 0:
                    continue
                orders[x["orderId"]] = {"side": x.get("side"), "qty": qty, "value": float(x.get("cumExecValue") or 0),
                                        "fee": float(x.get("cumExecFee") or 0), "fee_ccy": "",
                                        "ts": float(x.get("updatedTime") or x.get("createdTime") or 0) / 1000,
                                        "link": x.get("orderLinkId") or ""}
        src = "история ордеров"
    return sorted(orders.values(), key=lambda o: o["ts"]), src


def classify(cfg, o):
    """dca | dip-N% | other. Новые ордера — по метке orderLinkId, старые — по сумме ордера."""
    link = o.get("link", "")
    if link.startswith(BOT_TAG + "-"):
        r = link.split("-")[1]
        if r == "dca":
            return "dca"
        if r.startswith("dip"):
            return f"dip-{r[3:]}%"
        return "other"
    from strategy import base_order_size
    v = o["value"]
    if abs(v - base_order_size(cfg)) <= max(0.5, 0.05 * v):
        return "dca"
    for lvl in cfg["dips"]["levels"]:
        if abs(v - lvl["order_usdt"]) <= max(0.5, 0.05 * v):
            return f"dip-{lvl['drawdown_pct']}%"
    return "other"


def rebuild_state_from_exchange(cfg, client, days=90):
    """Восстанавливает потрачено/BTC/себестоимость/докупки/время последней покупки по сделкам на бирже.
    Учитывает ВСЕ покупки BTCUSDT на этом счёте (в том числе ручные) — так бот точно не купит второй раз."""
    orders, src = fetch_spot_buys(client, cfg["symbol"], days)
    st = State()
    now = time.time()
    for o in orders:
        if o["side"] == "Buy":
            kind = classify(cfg, o)
            fee_btc = o["fee"] if o["fee_ccy"] in ("", "BTC") else 0.0
            st.spent_usdt += o["value"]; st.btc += o["qty"] - fee_btc; st.cost_basis_usdt += o["value"]
            if kind == "dca" or kind == "other":
                st.last_base_buy_ts = max(st.last_base_buy_ts or 0, o["ts"])
            if kind.startswith("dip"):
                st.dip_spent_usdt += o["value"]
                lvl = float(kind[4:-1]); lvl = int(lvl) if lvl.is_integer() else lvl
                st.dip_level_ts[str(lvl)] = max(float(st.dip_level_ts.get(str(lvl), 0)), o["ts"])
                if now - o["ts"] < cfg["dips"]["lookback_days"] * 86400 and lvl not in st.dip_levels_hit:
                    st.dip_levels_hit.append(lvl)
        elif o["side"] == "Sell":
            avg = st.avg_entry or 0
            fee_usdt = o["fee"] if o["fee_ccy"] in ("", "USDT") else 0.0
            st.btc -= o["qty"]; st.cost_basis_usdt -= avg * o["qty"]; st.received_usdt += o["value"] - fee_usdt
            st.last_tp_ts = o["ts"]
        st.trades += 1
    st.btc = max(st.btc, 0.0)
    st.synced_from_exchange = True
    return st, src, len(orders)


def sync_if_needed(cfg, client, st, has_history, state_path):
    """Если локальной истории нет (новая папка, удалён/пустой файл) — берём её с биржи.
    Если на бирже потрачено больше, чем знает файл (бот запускали из другой папки) — тоже берём с биржи.
    Возвращает (state, можно_торговать)."""
    try:
        ex, src, n = rebuild_state_from_exchange(cfg, client)
    except Exception as e:
        if has_history:
            print(f"  Не смог сверить историю с биржей ({str(e)[:120]}), работаю по локальному файлу.")
            return st, True
        print(f"  Нет локальной истории и не удалось прочитать сделки с биржи ({str(e)[:120]}).\n"
              f"  Покупки пропускаю, чтобы не купить второй раз; попробую снова через час.", file=sys.stderr)
        return st, False
    if not has_history or ex.spent_usdt > st.spent_usdt + 1.0:
        why = "локального файла нет" if not has_history else \
              f"на бирже потрачено {ex.spent_usdt:.2f}, а в файле {st.spent_usdt:.2f}"
        if has_history:
            # сохраняем отметки уровней из файла, если они свежее
            for k, v in (st.dip_level_ts or {}).items():
                ex.dip_level_ts[k] = max(float(ex.dip_level_ts.get(k, 0)), float(v))
            ex.last_base_buy_ts = max(ex.last_base_buy_ts or 0, st.last_base_buy_ts or 0) or None
            ex.dip_levels_hit = sorted(set(ex.dip_levels_hit) | set(st.dip_levels_hit))
        print(f"  История восстановлена с биржи ({src}, {why}): ордеров {n}, потрачено {ex.spent_usdt:.2f} USDT, "
              f"BTC {ex.btc:.6f}, на просадках {ex.dip_spent_usdt:.2f} USDT")
        if ex.last_base_buy_ts:
            print(f"  последняя регулярная покупка: {time.strftime('%Y-%m-%d %H:%M', time.localtime(ex.last_base_buy_ts))}")
        save_state(ex, state_path)
        return ex, True
    return st, True


def step(cfg, client, st, state_path, testnet):
    sym = cfg["symbol"]
    price = float(client.get_tickers(category="spot", symbol=sym)["result"]["list"][0]["lastPrice"])
    kl = client.get_kline(category="spot", symbol=sym, interval="D", limit=cfg["dips"]["lookback_days"])
    recent_high = max(float(k[4]) for k in kl["result"]["list"])
    actions = decide(cfg, st, time.time(), price, recent_high)
    tag = "DEMO" if testnet else "LIVE"
    print(f"[{tag}] цена {price:.2f}, макс. за {cfg['dips']['lookback_days']}д {recent_high:.2f}, "
          f"потрачено {st.spent_usdt:.2f}/{cfg['limits']['max_total_budget_usdt']} USDT, действий: {len(actions)}")
    for side, amt, reason in actions:
        if side == "buy":
            r = client.place_order(category="spot", symbol=sym, side="Buy", orderType="Market",
                                   qty=f"{amt:.2f}", marketUnit="quoteCoin", isLeverage=0,
                                   orderLinkId=link_id(reason))
        else:
            qty = math.floor(amt * 1e6) / 1e6
            if qty <= 0:
                continue
            r = client.place_order(category="spot", symbol=sym, side="Sell", orderType="Market",
                                   qty=f"{qty:.6f}", marketUnit="baseCoin", isLeverage=0,
                                   orderLinkId=link_id(reason))
        oid = r["result"]["orderId"]
        o = None
        for _ in range(6):  # на демо-счёте ордер появляется в истории с задержкой
            time.sleep(2)
            for fn in (client.get_order_history, client.get_open_orders):
                try:
                    lst = fn(category="spot", symbol=sym, orderId=oid)["result"]["list"]
                except Exception:
                    lst = []
                if lst and float(lst[0].get("cumExecQty") or 0) > 0:
                    o = lst[0]; break
            if o:
                break
        if o is None:
            # ордер отправлен, но исполнение не подтвердилось: учитываем по заявке, чтобы не купить повторно
            print(f"  {side} ({reason}): ордер {oid} отправлен, исполнение не подтверждено, учитываю по заявке")
            o = {"cumExecQty": (amt / price) if side == "buy" else amt,
                 "cumExecValue": amt if side == "buy" else amt * price, "cumExecFee": 0}
        filled_btc = float(o["cumExecQty"]); filled_usdt = float(o["cumExecValue"])
        fee = float(o.get("cumExecFee") or 0)
        if side == "buy":
            # на споте Bybit комиссия покупки берётся в BTC
            if reason.startswith("dip"):
                st.dip_spent_usdt += filled_usdt
            st.spent_usdt += filled_usdt; st.btc += filled_btc - fee; st.cost_basis_usdt += filled_usdt
        else:
            avg = st.avg_entry or 0
            st.btc -= filled_btc; st.cost_basis_usdt -= avg * filled_btc; st.received_usdt += filled_usdt - fee
        st.trades += 1
        print(f"  {side} ({reason}): {filled_btc:.6f} BTC на {filled_usdt:.2f} USDT, ордер {oid}")
        save_state(st, state_path)
    save_state(st, state_path)


def cmd_trade(cfg, a, testnet):
    if not testnet:
        if not (cfg["mode"].get("live_enabled") and a.i_understand_real_money):
            sys.exit("Режим live выключен. Нужно И live_enabled: true в config.yaml, И флаг "
                     "--i-understand-real-money. Сначала проверьте всё на testnet.")
    client = make_client(testnet)
    sp = os.path.join(HERE, ("testnet_" if testnet else "live_") + cfg["mode"]["state_file"])
    st, has_history = load_state(sp)
    print(f"Файл состояния: {sp}")
    synced = False
    while True:
        try:
            if not synced:
                st, synced = sync_if_needed(cfg, client, st, has_history, sp)
            if synced:
                step(cfg, client, st, sp, testnet)
        except Exception as e:
            print("Ошибка шага:", e, file=sys.stderr)
            if "403" in str(e):
                print("Похоже, Bybit блокирует ваш IP/страну (или превышен лимит запросов). "
                      "Запускайте бота с компьютера в стране, где Bybit доступен.", file=sys.stderr)
            if a.once:
                sys.exit(1)
        if a.once:
            break
        time.sleep(3600)


def main():
    ap = argparse.ArgumentParser(description="DCA-бот Bybit spot BTC/USDT")
    ap.add_argument("mode", choices=["fetch", "backtest", "testnet", "live"])
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--tf", default="1d", choices=["1d", "4h"])
    ap.add_argument("--chart", default=os.path.join(HERE, "backtest.png"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--i-understand-real-money", action="store_true")
    a = ap.parse_args()
    os.chdir(HERE)
    cfg = load_cfg(a.config)
    if a.mode == "fetch":
        import subprocess; subprocess.run([sys.executable, "fetch_data.py"], check=True)
    elif a.mode == "backtest":
        cmd_backtest(cfg, a)
    else:
        if not (a.once or a.loop):
            a.once = True
        cmd_trade(cfg, a, testnet=(a.mode == "testnet"))


if __name__ == "__main__":
    main()
