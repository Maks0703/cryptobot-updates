"""Backtest of simple long/short BTCUSDT perpetual strategies at 2x / 3x leverage.

Data: Binance spot BTCUSDT 4h klines (proxy for perp price) + real Binance USDT-M
BTCUSDT funding history (data.binance.vision). Missing funding -> 0.01%/8h assumed.
Signals on the closed candle, execution at the next candle's open. No lookahead.
Costs: taker 0.055%/side, slippage 0.02%/fill, funding every 8h, isolated-margin liquidation.
Tuning on first 70% (in-sample, by Sharpe), report on last 30% (out-of-sample).
"""
import itertools, numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt

FEE, SLIP, MMR, DEF_FUND, START = 0.00055, 0.0002, 0.005, 0.0001, 1000.0
RISK = 0.02

# ---------------- data ----------------
def load():
    df = pd.read_csv("data/BTCUSDT_4h_long.csv")
    df["time"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    f = pd.read_csv("data/BTCUSDT_funding.csv")
    f["hour"] = (f.calc_time // 3_600_000) * 3_600_000
    fmap = dict(zip(f.hour, f.last_funding_rate))
    last_f = f.calc_time.max()
    hours = df.ts.values
    is_f = (pd.to_datetime(hours, unit="ms", utc=True).hour % 8 == 0)
    rate = np.array([fmap.get(h, DEF_FUND) for h in hours])
    df["fund_rate"] = np.where(is_f, rate, 0.0)
    df["fund_assumed"] = is_f & np.array([h not in fmap for h in hours])
    return df

# ---------------- indicators ----------------
def atr(df, n=14):
    pc = df.close.shift()
    tr = pd.concat([df.high-df.low, (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1+up/dn)

def supertrend_dir(df, n, mult):
    a = atr(df, n).values; hl2 = ((df.high+df.low)/2).values; c = df.close.values
    ub, lb = hl2+mult*a, hl2-mult*a; fu, fl = ub.copy(), lb.copy(); d = np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = lb[i] if (lb[i] > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]
        d[i] = 1 if c[i] > fu[i-1] else (-1 if c[i] < fl[i-1] else d[i-1])
    return d

# ---------------- strategies: return entry_long, entry_short, exit_long, exit_short (at close i) ----------------
def s_ema(df, fast, slow):
    d = np.sign(df.close.ewm(span=fast, adjust=False).mean() - df.close.ewm(span=slow, adjust=False).mean()).values
    pd_ = np.roll(d, 1); pd_[0] = 0
    up, dn = (d > 0) & (pd_ <= 0), (d < 0) & (pd_ >= 0)
    up[:slow] = dn[:slow] = False
    return up, dn, dn, up

def s_donchian(df, n, m):
    hh, ll = df.high.rolling(n).max().shift(), df.low.rolling(n).min().shift()
    xh, xl = df.high.rolling(m).max().shift(), df.low.rolling(m).min().shift()
    c = df.close
    return (c > hh).values, (c < ll).values, (c < xl).values, (c > xh).values

def s_super(df, n, mult):
    d = supertrend_dir(df, n, mult); pd_ = np.roll(d, 1); pd_[0] = d[0]
    up, dn = (d > 0) & (pd_ < 0), (d < 0) & (pd_ > 0)
    return up, dn, dn, up

def s_rsi(df, lo, hi, trend):
    r = rsi(df.close).values; ema = df.close.ewm(span=200, adjust=False).mean().values; c = df.close.values
    el, es = r < lo, r > hi
    if trend: el, es = el & (c > ema), es & (c < ema)
    return el, es, r > 50, r < 50

STRATS = {
    "EMA cross":  (s_ema,      [(20, 50), (20, 100), (50, 200)]),
    "Donchian":   (s_donchian, [(20, 10), (55, 20), (100, 50)]),
    "Supertrend": (s_super,    [(10, 3), (14, 4), (20, 5)]),
    "RSI revert": (s_rsi,      [(30, 70, False), (30, 70, True), (25, 75, True)]),
}
STOP_K = [3, 5]          # trailing stop = k * ATR(14) from best close since entry
STOP_K_RSI = [2, 3]

# ---------------- engine ----------------
def run(df, sig, lev, k, sizing, i0, i1):
    """sizing: 'risk' = risk RISK of equity per trade to the stop (notional capped at lev*equity);
               'full' = notional = lev * equity every trade."""
    el, es, xl, xs = sig
    o, h, l, c = df.open.values, df.high.values, df.low.values, df.close.values
    A = atr(df).values; fr = df.fund_rate.values; t = df.time.values
    eq = START; pos = 0; qty = 0.0; entry = stop = liq = best = 0.0; margin = 0.0
    eq_at_entry = 0.0; trade_pnl = 0.0
    pending = None; curve = np.empty(i1-i0); trades = []; liqs = 0; fund_total = 0.0; fees_total = 0.0

    def close_pos(px, reason):
        nonlocal eq, pos, qty, trade_pnl, fees_total
        fill = px*(1 - SLIP*pos)
        pnl = pos*qty*(fill-entry); fee = qty*fill*FEE
        eq += pnl - fee; fees_total += fee; trade_pnl += pnl - fee
        trades.append(dict(side=pos, ret=trade_pnl/eq_at_entry, reason=reason)); pos = 0; qty = 0.0

    for j, i in enumerate(range(i0, i1)):
        # funding at bar open (timestamps 00/08/16 UTC) on open position
        if pos and fr[i]:
            pay = pos*qty*o[i]*fr[i]; eq -= pay; trade_pnl -= pay; fund_total += pay
        # gap at open through liq / stop
        if pos:
            if (pos > 0 and o[i] <= liq) or (pos < 0 and o[i] >= liq):
                eq -= margin + trade_pnl*0  # lose whole isolated margin
                trades.append(dict(side=pos, ret=(-margin+trade_pnl)/eq_at_entry, reason="liq")); liqs += 1
                pos = 0; qty = 0.0
            elif (pos > 0 and o[i] <= stop) or (pos < 0 and o[i] >= stop):
                close_pos(o[i], "stop-gap")
        # pending orders from previous close
        if pending:
            act, new = pending; pending = None
            if pos and act == "exit": close_pos(o[i], "signal")
            if new and not pos and eq > 0:
                px = o[i]*(1 + SLIP*new); dist = k*A[i-1]
                cap = lev*eq/px
                qty = min(RISK*eq/dist, cap) if sizing == "risk" else cap
                fee = qty*px*FEE; eq -= fee; fees_total += fee
                pos, entry, best = new, px, px; eq_at_entry = eq + fee; trade_pnl = -fee
                stop = px - new*dist
                margin = qty*px/lev
                liq = px*(1 - new*(1/lev - MMR))
        # intrabar stop / liquidation
        if pos:
            hit_liq = (l[i] <= liq) if pos > 0 else (h[i] >= liq)
            hit_stop = (l[i] <= stop) if pos > 0 else (h[i] >= stop)
            stop_first = (stop > liq) if pos > 0 else (stop < liq)
            if hit_stop and (stop_first or not hit_liq):
                close_pos(stop, "stop")
            elif hit_liq:
                eq -= margin; liqs += 1
                trades.append(dict(side=pos, ret=(-margin+trade_pnl)/eq_at_entry, reason="liq")); pos = 0; qty = 0.0
        # mark to market at close, trail stop, signals
        mtm = eq + (pos*qty*(c[i]-entry) if pos else 0.0)
        curve[j] = max(mtm, 0.0)
        if pos:
            if pos > 0: best = max(best, c[i]); stop = max(stop, best - k*A[i])
            else: best = min(best, c[i]); stop = min(stop, best + k*A[i])
        if i < i1-1 and eq > 0:
            if pos > 0 and (xl[i] or es[i]): pending = ("exit", -1 if es[i] else 0)
            elif pos < 0 and (xs[i] or el[i]): pending = ("exit", 1 if el[i] else 0)
            elif not pos and el[i]: pending = (None, 1)
            elif not pos and es[i]: pending = (None, -1)
    if pos: close_pos(c[i1-1], "end")
    curve[-1] = max(eq, 0.0)
    return pd.Series(curve, index=pd.to_datetime(t[i0:i1])), trades, liqs, fund_total, fees_total

def metrics(curve, trades=None, liqs=0):
    daily = curve.resample("1D").last().dropna()
    r = daily.pct_change().dropna()
    yrs = (curve.index[-1]-curve.index[0]).days/365.25
    tot = curve.iloc[-1]/curve.iloc[0]-1
    m = dict(total_ret_pct=100*tot,
             cagr_pct=100*((1+tot)**(1/yrs)-1) if tot > -1 else -100,
             max_dd_pct=100*(curve/curve.cummax()-1).min(),
             sharpe=(r.mean()/r.std()*np.sqrt(365)) if r.std() > 0 else 0.0)
    if trades is not None:
        rets = [x["ret"] for x in trades]
        m.update(trades=len(trades), win_rate_pct=100*np.mean([x > 0 for x in rets]) if rets else 0,
                 liquidations=liqs, worst_trade_pct=100*min(rets) if rets else 0)
    return m

def buy_hold(df, i0, i1):
    c = df.close.values[i0:i1]; px = df.open.values[i0]*(1+SLIP)
    q = START*(1-FEE)/px
    return pd.Series(q*c, index=pd.to_datetime(df.time.values[i0:i1]))

def dca(df, i0, i1):
    """Spend START evenly once per day (00:00 UTC bar) over the period; 0.1% spot fee."""
    sub = df.iloc[i0:i1]; days = sub[sub.time.dt.hour == 0]; per = START/len(days)
    cash, btc, vals = START, 0.0, []
    buy_idx = set(days.index)
    for idx, row in sub.iterrows():
        if idx in buy_idx: btc += per*(1-0.001)/(row.open*(1+SLIP)); cash -= per
        vals.append(cash + btc*row.close)
    return pd.Series(vals, index=sub.time.values)

def main():
    df = load()
    warm = 250; n = len(df); split = warm + int(0.7*(n-warm))
    print(f"Data {df.time.iloc[warm]} .. {df.time.iloc[-1]}  bars={n}  split at {df.time.iloc[split]}")
    print(f"Funding bars assumed 0.01%: {int(df.fund_assumed.sum())}; real funding mean/8h = {df.fund_rate[df.fund_rate!=0].mean()*100:.4f}%")
    rows, chosen, curves = [], {}, {}
    # tune on in-sample (risk sizing, 3x cap), by Sharpe
    for name, (fn, grid) in STRATS.items():
        ks = STOP_K_RSI if name == "RSI revert" else STOP_K
        best = None
        for p, k in itertools.product(grid, ks):
            sig = fn(df, *p)
            cv, tr, lq, *_ = run(df, sig, 3, k, "risk", warm, split)
            mm = metrics(cv, tr, lq)
            rows.append(dict(strategy=name, params=str(p), stop_k=k, phase="IS-tuning", **mm))
            if best is None or mm["sharpe"] > best[0]: best = (mm["sharpe"], p, k)
        chosen[name] = best[1:]
        print(f"{name}: chosen params {best[1]} stop {best[2]}xATR (IS Sharpe {best[0]:.2f})")
    res = []
    for phase, (a, b) in {"in-sample": (warm, split), "out-of-sample": (split, n)}.items():
        for name, (p, k) in chosen.items():
            sig = STRATS[name][0](df, *p)
            for sizing in ["risk", "full"]:
                for lev in [2, 3]:
                    cv, tr, lq, fund, fees = run(df, sig, lev, k, sizing, a, b)
                    res.append(dict(phase=phase, strategy=name, params=str(p), stop_k=k,
                                    sizing="2% risk" if sizing == "risk" else "full lev", leverage=lev,
                                    **metrics(cv, tr, lq), funding_paid=fund, fees_paid=fees))
                    if phase == "out-of-sample": curves[f"{name} {lev}x {sizing}"] = cv
        bh = buy_hold(df, a, b); dc = dca(df, a, b)
        res.append(dict(phase=phase, strategy="Buy & hold BTC", leverage=1, **metrics(bh)))
        res.append(dict(phase=phase, strategy="Daily DCA (spot)", leverage=1, **metrics(dc)))
        if phase == "out-of-sample": curves["Buy & hold"] = bh; curves["DCA"] = dc
    R = pd.DataFrame(res); pd.DataFrame(rows).to_csv("results/futures_tuning_insample.csv", index=False)
    R.to_csv("results/futures_backtest.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    cols = ["strategy", "sizing", "leverage", "total_ret_pct", "cagr_pct", "max_dd_pct", "sharpe",
            "win_rate_pct", "trades", "liquidations", "worst_trade_pct", "funding_paid", "fees_paid"]
    for ph in ["in-sample", "out-of-sample"]:
        print(f"\n=== {ph} ===  ({df.time.iloc[warm if ph=='in-sample' else split].date()} .. "
              f"{df.time.iloc[split-1 if ph=='in-sample' else n-1].date()})")
        print(R[R.phase == ph][cols].round(2).to_string(index=False))
    # chart
    fig, ax = plt.subplots(2, 1, figsize=(13, 10), sharex=True)
    for key, cv in curves.items():
        if "full" in key: continue
        if key in ("Buy & hold", "DCA"): ax[0].plot(cv.index, cv.values, "k--" if key == "Buy & hold" else "k:", lw=2, label=key)
        else: ax[0].plot(cv.index, cv.values, label=key.replace(" risk", " (2% risk)"))
    for key, cv in curves.items():
        if "risk" in key: continue
        if key in ("Buy & hold", "DCA"): ax[1].plot(cv.index, cv.values, "k--" if key == "Buy & hold" else "k:", lw=2, label=key)
        else: ax[1].plot(cv.index, cv.values, label=key.replace(" full", " (full leverage)"))
    ax[0].set_title("Out-of-sample equity, $1000 start — 2% risk per trade sizing")
    ax[1].set_title("Out-of-sample equity, $1000 start — full leverage sizing (notional = lev x equity)")
    for a_ in ax: a_.legend(fontsize=8, ncol=2); a_.grid(alpha=.3); a_.set_ylabel("USD")
    plt.tight_layout(); plt.savefig("futures_backtest.png", dpi=110)
    print("\nchart: /workspace/cryptobot/futures_backtest.png")

if __name__ == "__main__":
    main()
