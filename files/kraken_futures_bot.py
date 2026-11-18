#!/usr/bin/env python3
"""Kraken FUTURES PAPER bot: EMA50/200 trend, LONG and SHORT, 8 altcoin perps. PAPER ONLY.

Signal (identical to alts_short_backtest.py "EMA short (50,200)", mirrored for longs):
  short cond = EMA50 < EMA200 and close < EMA200 ; long cond = EMA50 > EMA200 and close > EMA200
  entry  = rising edge of cond on a CLOSED 4h bar (fresh only; never join old trends)
  exit   = cond no longer true on the last closed 4h bar (signal flip) -> market close
  stop   = entry -/+ 5*ATR(14) of signal bar; trailed each closed bar from best close:
           short: stop = min(stop, min_close + 5*ATR) ; long: stop = max(stop, max_close - 5*ATR)
  size   = 2% equity risk / stop distance * news size_mult, notional cap 3x equity per position,
           max 4 open positions, total notional cap 3x equity, one position per coin.
Signal data: Binance spot USDT 4h klines (data-api.binance.vision), same source as the backtest.
Execution: `kraken futures paper` (simulated, local). Stops are paper reduce-only stop orders
(trigger: mark) that only evaluate when the CLI runs, so every run also checks Kraken 1h candles
since the last run and closes at market if the stop was breached in between.
Modes: (default) one hourly run | --report daily summary | --weekly journal stats
"""
import csv, json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(BASE, "state", "futures_bot_state.json")
NEWS_F = os.path.join(BASE, "state", "news_gate.json")
JOURNAL_F = os.path.join(BASE, "journal", "futures_journal.csv")
COINS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK"]
PREC = {"ETH": 3, "BNB": 2, "SOL": 2, "LINK": 1, "AVAX": 1, "XRP": 0, "ADA": 0, "DOGE": 0}
FAST, SLOW, ATR_N, STOP_K = 50, 200, 14, 5.0
RISK, LEV, MAX_POS, TOTAL_NOTIONAL_X = 0.02, 3.0, 4, 3.0
START_BAL, FEE_RATE = 1000, 0.0005
BAR_MS = 14_400_000
BERLIN = ZoneInfo("Europe/Berlin")
COLS = ["ts_utc", "coin", "symbol", "direction", "action", "reason", "bar_time", "close", "ema50", "ema200",
        "atr", "price", "size", "notional", "stop", "equity", "news_block", "news_mult", "news_reason",
        "pnl_usd", "pnl_pct_equity", "outcome", "order_id"]

def sym(c): return f"PF_{c}USD"
def now_utc(): return datetime.now(timezone.utc)

# ---------------- infra ----------------
def kraken(*args, check=True):
    env = dict(os.environ); env.pop("KRAKEN_WORKSPACE", None)  # futures paper is not workspace-scoped
    for a in args:
        if a in ("order", "live", "promote") and args[0] != "futures":
            raise RuntimeError("refusing non-paper command")
    if args[0] == "futures" and args[1] != "paper" and args[1] not in ("ticker", "tickers", "instruments"):
        raise RuntimeError("refusing non-paper futures command")
    r = subprocess.run(["kraken", *args, "-o", "json"], capture_output=True, text=True, env=env, timeout=60)
    out = r.stdout.strip()
    try: d = json.loads(out.splitlines()[-1]) if out else {}
    except Exception: d = {"error": "parse", "message": out[:300]}
    if check and (r.returncode != 0 or "error" in d):
        raise RuntimeError(f"kraken {' '.join(args)} -> {d or r.stderr[:300]}")
    return d

def fp(*a, check=True): return kraken("futures", "paper", *a, check=check)

def http_json(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cryptobot"}), timeout=20) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1: raise
            time.sleep(2)

def load_state():
    if os.path.exists(STATE_F): return json.load(open(STATE_F))
    return {"positions": {}, "shadow": {}, "last_bar": {}, "last_check_ms": None}

def save_state(s):
    tmp = STATE_F + ".tmp"; json.dump(s, open(tmp, "w"), indent=1); os.replace(tmp, STATE_F)

def journal(row):
    new = not os.path.exists(JOURNAL_F)
    with open(JOURNAL_F, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new: w.writeheader()
        r = {k: "" for k in COLS}; r.update({k: v for k, v in row.items() if k in COLS})
        r["ts_utc"] = now_utc().isoformat(timespec="seconds")
        for k, v in r.items():
            if isinstance(v, float): r[k] = f"{v:.6g}" if abs(v) < 1 else round(v, 4)
        w.writerow(r)

def news_gate():
    g = {"block_new_entries": False, "size_mult": 1.0, "reason": "no news_gate.json (default allow)", "updated": None}
    try:
        d = json.load(open(NEWS_F)); g.update(d)
        g["size_mult"] = float(min(max(float(g.get("size_mult", 1)), 0.0), 1.0))
        if g.get("updated"):
            age = now_utc() - datetime.fromisoformat(str(g["updated"]).replace("Z", "+00:00"))
            if age > timedelta(hours=26): g["reason"] = f"{g.get('reason','')} [STALE {age.total_seconds()/3600:.0f}h]"
    except FileNotFoundError: pass
    except Exception as e: g["reason"] = f"news_gate.json unreadable ({e}); default allow"
    return g

# ---------------- signals ----------------
def ema(x, n): return x.ewm(span=n, adjust=False).mean()

def candles(coin):
    k = http_json(f"https://data-api.binance.vision/api/v3/klines?symbol={coin}USDT&interval=4h&limit=1000")
    d = pd.DataFrame(k, columns=list("tohlcv") + ["ct", "q", "n", "tb", "tq", "x"])
    d = d[["t", "o", "h", "l", "c", "ct"]].astype(float)
    d = d[d.ct < time.time() * 1000].reset_index(drop=True)  # closed bars only
    d["t"] = d.t.astype("int64")
    pc = d.c.shift()
    tr = pd.concat([d.h - d.l, (d.h - pc).abs(), (d.l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / ATR_N, adjust=False).mean()
    d["ef"], d["es"] = ema(d.c, FAST), ema(d.c, SLOW)
    d["short"] = (d.ef < d.es) & (d.c < d.es)
    d["long"] = (d.ef > d.es) & (d.c > d.es)
    return d

def bar_iso(ms): return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="minutes")

def kraken_hl_since(symbol, since_ms):
    """max high / min low of Kraken perp 1h trade candles since since_ms (incl. current bar)."""
    frm = int(since_ms // 1000) - 3600
    d = http_json(f"https://futures.kraken.com/api/charts/v1/trade/{symbol}/1h?from={frm}")
    cs = [c for c in d.get("candles", []) if c["time"] + 3_600_000 > since_ms]
    if not cs: return None, None
    return max(float(c["high"]) for c in cs), min(float(c["low"]) for c in cs)

# ---------------- paper account helpers ----------------
def ensure_account():
    st = fp("status", check=False)
    if "error" in st and "not initialized" in st.get("message", ""):
        fp("init", "--balance", str(START_BAL), "--fee-rate", str(FEE_RATE))
        st = fp("status")
        return st, True
    if "error" in st: raise RuntimeError(st)
    return st, False

def open_positions():
    return {p["symbol"]: p for p in fp("positions").get("positions", [])}

def open_orders():
    return fp("orders").get("open_orders", [])

def close_pnl_from_fills(symbol, after_iso):
    fills = [f for f in fp("fills").get("fills", []) if f["symbol"] == symbol and f["filled_at"] >= after_iso]
    closes = [f for f in fills if f.get("realized_pnl") is not None]
    if not closes: return None, None, 0.0
    px = sum(f["price"] * f["size"] for f in closes) / sum(f["size"] for f in closes)
    return px, sum(f["realized_pnl"] for f in closes), sum(f["fee"] for f in closes)

# ---------------- main run ----------------
def run():
    st, inited = ensure_account()
    S = load_state(); gate = news_gate(); actions = []; errors = []
    pos_k = open_positions(); orders = open_orders()
    equity = float(st["equity"])
    nowms = int(time.time() * 1000)
    last_check = S.get("last_check_ms") or nowms - 3_600_000

    def jbase(coin, d=None, direction=""):
        r = {"coin": coin, "symbol": sym(coin), "direction": direction, "equity": equity,
             "news_block": gate["block_new_entries"], "news_mult": gate["size_mult"], "news_reason": gate["reason"]}
        if d is not None:
            b = d.iloc[-1]; r.update(bar_time=bar_iso(b.t), close=b.c, ema50=b.ef, ema200=b.es, atr=b.atr)
        return r

    def finish_trade(coin, p, exit_px, pnl_gross, exit_fee, action, reason, d=None, oid=""):
        net = pnl_gross - p.get("entry_fee", 0) - exit_fee
        row = jbase(coin, d, p["dir"]); row.update(action=action, reason=reason, price=exit_px, size=p["size"],
              stop=p["stop"], pnl_usd=net, pnl_pct_equity=100 * net / p["equity_at_entry"],
              outcome="win" if net > 0 else "loss", order_id=oid)
        journal(row); actions.append({"coin": coin, "action": action, "dir": p["dir"], "price": exit_px, "pnl_usd": round(net, 2), "reason": reason})
        for o in open_orders():
            if o["symbol"] == sym(coin): fp("cancel", "--order-id", o["id"], check=False)
        S["positions"].pop(coin, None)

    data = {}
    for c in COINS:
        try: data[c] = candles(c)
        except Exception as e: errors.append(f"{c} data: {e}")

    # 1) reconcile tracked positions with paper account (stop may have triggered inside the CLI)
    for coin, p in list(S["positions"].items()):
        if sym(coin) not in pos_k:
            px, pnl, fee = close_pnl_from_fills(sym(coin), p["opened_at"])
            finish_trade(coin, p, px or p["stop"], pnl or 0.0, fee, "exit", "paper stop order triggered (mark)", data.get(coin))

    # 2) stop breach between runs (paper stops are only checked when the CLI runs)
    for coin, p in list(S["positions"].items()):
        try: hi, lo = kraken_hl_since(sym(coin), max(last_check, p["opened_ms"]))
        except Exception as e: errors.append(f"{coin} 1h check: {e}"); continue
        breached = hi is not None and ((p["dir"] == "short" and hi >= p["stop"]) or (p["dir"] == "long" and lo <= p["stop"]))
        if breached:
            side = "buy" if p["dir"] == "short" else "sell"
            r = fp(side, sym(coin), str(p["size"]), "--type", "market", "--reduce-only", check=False)
            if "error" in r:   # maybe the paper stop already fired during this call's reconcile
                if sym(coin) not in open_positions():
                    px, pnl, fee = close_pnl_from_fills(sym(coin), p["opened_at"])
                    finish_trade(coin, p, px or p["stop"], pnl or 0.0, fee, "exit", "stop triggered", data.get(coin))
                else: errors.append(f"{coin} stop close failed: {r}")
                continue
            f = r["fills"]; px = sum(x["price"] * x["size"] for x in f) / sum(x["size"] for x in f)
            finish_trade(coin, p, px, sum(x["realized_pnl"] or 0 for x in f), sum(x["fee"] for x in f), "exit",
                         f"stop {p['stop']:.6g} breached between runs (1h {'high' if p['dir']=='short' else 'low'} "
                         f"{hi if p['dir']=='short' else lo:.6g}); closed at market", data.get(coin), r.get("order_id", ""))

    # 3) per-coin new closed bars: signal-flip exits, stop trailing, fresh entries
    pos_k = open_positions()
    notional = sum(abs(v["size"]) * v["mark_price"] for v in pos_k.values())
    for coin in COINS:
        d = data.get(coin)
        if d is None: continue
        last_t = int(d.t.iloc[-1]); prev_seen = S["last_bar"].get(coin)
        if prev_seen is not None and last_t <= prev_seen: continue   # no new closed 4h bar for this coin
        new_idx = list(d.index[d.t > prev_seen]) if prev_seen else [d.index[-1]]
        new_idx = new_idx[-2:]  # tolerate one missed bar at most
        b = d.iloc[-1]
        p = S["positions"].get(coin)
        if p:
            cond = bool(b[p["dir"]])
            if not cond:
                side = "buy" if p["dir"] == "short" else "sell"
                r = fp(side, sym(coin), str(p["size"]), "--type", "market", "--reduce-only", check=False)
                if "error" in r: errors.append(f"{coin} flip close: {r}")
                else:
                    f = r["fills"]; px = sum(x["price"] * x["size"] for x in f) / sum(x["size"] for x in f)
                    finish_trade(coin, p, px, sum(x["realized_pnl"] or 0 for x in f), sum(x["fee"] for x in f),
                                 "exit", "signal flip (EMA condition ended on closed 4h bar)", d, r.get("order_id", ""))
            else:
                for i in new_idx:
                    bb = d.loc[i]
                    if p["dir"] == "short":
                        p["best"] = min(p["best"], bb.c); new = min(p["stop"], p["best"] + STOP_K * bb.atr)
                    else:
                        p["best"] = max(p["best"], bb.c); new = max(p["stop"], p["best"] - STOP_K * bb.atr)
                if abs(new - p["stop"]) > 1e-12:
                    old = p["stop"]; p["stop"] = float(new)
                    r = fp("edit-order", "--order-id", p["stop_order"], "--stop-price", f"{new:.8g}", check=False)
                    if "error" in r: errors.append(f"{coin} stop edit: {r}")
                    row = jbase(coin, d, p["dir"]); row.update(action="stop_move", reason=f"trail 5xATR {old:.6g} -> {new:.6g}",
                               price=b.c, size=p["size"], stop=new, order_id=p["stop_order"]); journal(row)
                    actions.append({"coin": coin, "action": "stop_move", "dir": p["dir"], "from": round(old, 6), "to": round(new, 6)})
        # fresh entry?
        fresh = None
        for i in new_idx:
            if i == 0: continue
            for dr in ("short", "long"):
                if bool(d.loc[i, dr]) and not bool(d.loc[i - 1, dr]) and bool(b[dr]): fresh = dr
        # shadow (news-blocked) trades: update + maybe close
        sh = S["shadow"].get(coin)
        if sh:
            for i in new_idx:
                bb = d.loc[i]; hit = (bb.h >= sh["stop"]) if sh["dir"] == "short" else (bb.l <= sh["stop"])
                flip = not bool(bb[sh["dir"]])
                if hit or flip:
                    xp = sh["stop"] if hit else bb.c; sgn = -1 if sh["dir"] == "short" else 1
                    pnl = sgn * (xp - sh["entry"]) * sh["size"] - FEE_RATE * sh["size"] * (sh["entry"] + xp)
                    row = jbase(coin, d, sh["dir"]); row.update(action="shadow_exit", reason="news-blocked trade, hypothetical " + ("stop" if hit else "signal flip"),
                               price=xp, size=sh["size"], stop=sh["stop"], pnl_usd=pnl, pnl_pct_equity=100 * pnl / sh["equity"],
                               outcome="would_win" if pnl > 0 else "would_lose", news_reason=sh["news_reason"]); journal(row)
                    S["shadow"].pop(coin, None); sh = None; break
                if sh["dir"] == "short": sh["best"] = min(sh["best"], bb.c); sh["stop"] = min(sh["stop"], sh["best"] + STOP_K * bb.atr)
                else: sh["best"] = max(sh["best"], bb.c); sh["stop"] = max(sh["stop"], sh["best"] - STOP_K * bb.atr)
        S["last_bar"][coin] = last_t
        if not fresh:
            continue
        row = jbase(coin, d, fresh)
        dist = STOP_K * float(b.atr); px_now = float(b.c)
        if coin in S["positions"]:
            row.update(action="skip", reason="already in a position on this coin"); journal(row); continue
        if gate["block_new_entries"] or gate["size_mult"] <= 0:
            size = round(min(RISK * equity / dist, LEV * equity / px_now), PREC[coin])
            row.update(action="skip", reason="NEWS GATE blocked entry", price=px_now, size=size,
                       stop=px_now + dist if fresh == "short" else px_now - dist); journal(row)
            S["shadow"][coin] = {"dir": fresh, "entry": px_now, "size": size, "best": px_now, "stop": row["stop"],
                                 "equity": equity, "news_reason": gate["reason"]}
            actions.append({"coin": coin, "action": "skip_news", "dir": fresh}); continue
        if len(S["positions"]) >= MAX_POS:
            row.update(action="skip", reason=f"max {MAX_POS} open positions"); journal(row); continue
        tk = kraken("futures", "ticker", sym(coin))["ticker"]
        px = float(tk["bid"] if fresh == "short" else tk["ask"])
        size = min(RISK * equity * gate["size_mult"] / dist, LEV * equity / px, max(0.0, TOTAL_NOTIONAL_X * equity - notional) / px)
        size = round(size, PREC[coin])
        if size <= 0:
            row.update(action="skip", reason="total exposure cap reached"); journal(row); continue
        fp("set-leverage", sym(coin), str(int(LEV)))
        r = fp("sell" if fresh == "short" else "buy", sym(coin), str(size), "--type", "market", "--leverage", str(int(LEV)), check=False)
        if "error" in r:
            row.update(action="error", reason=f"entry failed: {r.get('message')}"); journal(row); errors.append(f"{coin} entry: {r}"); continue
        f = r["fills"]; fill = sum(x["price"] * x["size"] for x in f) / sum(x["size"] for x in f); fee = sum(x["fee"] for x in f)
        stop = fill + dist if fresh == "short" else fill - dist
        so = fp("buy" if fresh == "short" else "sell", sym(coin), str(size), "--type", "stop", "--stop-price", f"{stop:.8g}",
                "--reduce-only", "--trigger-signal", "mark", "--leverage", str(int(LEV)), check=False)
        S["positions"][coin] = {"dir": fresh, "size": size, "entry": fill, "best": fill, "stop": float(stop), "entry_fee": fee,
                                "stop_order": so.get("order_id", ""), "opened_at": now_utc().isoformat(), "opened_ms": nowms,
                                "equity_at_entry": equity, "signal_bar": bar_iso(b.t)}
        notional += size * fill
        row.update(action="entry", reason=f"fresh EMA{FAST}/{SLOW} {fresh} signal" + (f"; size x{gate['size_mult']}" if gate["size_mult"] < 1 else ""),
                   price=fill, size=size, notional=size * fill, stop=stop, order_id=r.get("order_id", ""))
        journal(row); actions.append({"coin": coin, "action": "entry", "dir": fresh, "price": fill, "size": size, "stop": round(stop, 6)})
        if "error" in so: errors.append(f"{coin} stop order: {so}")

    # cancel orphan orders (stops left on coins we no longer hold) so they can never hit a future position
    keep = {p.get("stop_order") for p in S["positions"].values()}
    for o in open_orders():
        if o["id"] not in keep:
            fp("cancel", "--order-id", o["id"], check=False); actions.append({"coin": o["symbol"], "action": "cancel_orphan_order", "id": o["id"]})
    S["last_check_ms"] = nowms; save_state(S)
    st = fp("status"); pos_k = open_positions()
    signals = {c: ("long" if bool(d.long.iloc[-1]) else "short" if bool(d.short.iloc[-1]) else "flat") for c, d in data.items()}
    print(json.dumps({"time_berlin": datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M"), "account_initialized_now": inited,
                      "actions": actions, "errors": errors, "news_gate": gate, "trend_state_last_4h_bar": signals,
                      "open_positions": [{"coin": c, **{k: p[k] for k in ("dir", "size", "entry", "stop")},
                                          "upnl": round(pos_k.get(sym(c), {}).get("unrealized_pnl", 0), 2)} for c, p in S["positions"].items()],
                      "equity": round(st["equity"], 2), "pnl_usd": round(st["pnl"], 2), "pnl_pct": round(st["pnl_pct"], 3)},
                     ensure_ascii=False, indent=1))

# ---------------- reports ----------------
def read_journal():
    if not os.path.exists(JOURNAL_F): return pd.DataFrame(columns=COLS)
    j = pd.read_csv(JOURNAL_F); j["ts"] = pd.to_datetime(j.ts_utc, utc=True); return j

def report():
    st = fp("status"); pos_k = open_positions(); S = load_state()
    j = read_journal(); today = datetime.now(BERLIN).date()
    jt = j[j.ts.dt.tz_convert(BERLIN).dt.date == today] if len(j) else j
    print(json.dumps({"date_berlin": str(today), "equity": round(st["equity"], 2), "start": st["starting_collateral"],
        "total_pnl_usd": round(st["pnl"], 2), "total_pnl_pct": round(st["pnl_pct"], 3), "unrealized_pnl": round(st["unrealized_pnl"], 2),
        "open_positions": [{"coin": c, "dir": p["dir"], "size": p["size"], "entry": p["entry"], "stop": p["stop"],
                            "mark": pos_k.get(sym(c), {}).get("mark_price"), "upnl_usd": round(pos_k.get(sym(c), {}).get("unrealized_pnl", 0), 2),
                            "opened_at": p["opened_at"]} for c, p in S["positions"].items()],
        "today_events": jt[["ts_utc", "coin", "direction", "action", "reason", "price", "size", "stop", "pnl_usd"]].fillna("").to_dict("records"),
        "today_realized_pnl_usd": round(float(pd.to_numeric(jt[jt.action == "exit"].pnl_usd, errors="coerce").sum()), 2) if len(jt) else 0.0,
        "news_gate": news_gate()}, ensure_ascii=False, indent=1, default=str))

def weekly():
    j = read_journal(); since = now_utc() - timedelta(days=7)
    w = j[j.ts >= since] if len(j) else j
    ex = w[w.action == "exit"].copy(); ex["pnl_usd"] = pd.to_numeric(ex.pnl_usd, errors="coerce")
    sx = w[w.action == "shadow_exit"].copy(); sx["pnl_usd"] = pd.to_numeric(sx.pnl_usd, errors="coerce")
    sk = w[w.action == "skip"]
    st = fp("status")
    print(json.dumps({"window_utc": [since.isoformat(timespec="minutes"), now_utc().isoformat(timespec="minutes")],
        "equity": round(st["equity"], 2), "total_pnl_usd": round(st["pnl"], 2),
        "entries": int((w.action == "entry").sum()), "closed_trades": len(ex),
        "win_rate_pct": round(100 * (ex.pnl_usd > 0).mean(), 1) if len(ex) else None,
        "realized_pnl_usd": round(float(ex.pnl_usd.sum()), 2),
        "pnl_by_coin": ex.groupby("coin").pnl_usd.sum().round(2).to_dict(),
        "pnl_by_direction": ex.groupby("direction").pnl_usd.sum().round(2).to_dict(),
        "exits_by_reason": ex.reason.str.slice(0, 40).value_counts().to_dict(),
        "stop_moves": int((w.action == "stop_move").sum()),
        "skips_by_reason": sk.reason.value_counts().to_dict(),
        "news_blocked_entries": int((sk.reason == "NEWS GATE blocked entry").sum()),
        "news_blocked_hypothetical_closed": len(sx), "news_blocked_hypothetical_pnl_usd": round(float(sx.pnl_usd.sum()), 2),
        "news_blocked_still_open_shadow": list(load_state()["shadow"].keys()),
        "news_gate_verdict": (None if not len(sx) else "gate SAVED money" if sx.pnl_usd.sum() < 0 else "gate COST money")},
        ensure_ascii=False, indent=1, default=str))

if __name__ == "__main__":
    os.makedirs(os.path.dirname(STATE_F), exist_ok=True); os.makedirs(os.path.dirname(JOURNAL_F), exist_ok=True)
    if "--report" in sys.argv: report()
    elif "--weekly" in sys.argv: weekly()
    else: run()
