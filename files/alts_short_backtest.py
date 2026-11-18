"""SHORT-only backtest on 8 altcoin USDT perpetuals (4h, 2021-01 .. 2026-11).

Prices: Binance spot 4h klines (data-api.binance.vision) used as proxy for perp prices.
Funding: real per-coin Binance USDT-M funding (data.binance.vision monthly archives,
2021-01 .. 2026-10). Bars after the last archived month -> 0.01%/8h assumed (flagged).
Shorts RECEIVE positive funding, PAY negative funding.
Strategies (short only, signal on closed bar, fill at next bar open):
  ST short   : Supertrend(n,mult) turns down while close < EMA200 -> short; exit when ST up or close > EMA200
  EMA short  : EMA fast < slow while close < EMA200 -> short; exit when condition ends
  Breakdown  : close < lowest low of prior N bars (opt. close<EMA200) -> short; exit close > highest high of prior M
All with trailing stop k*ATR(14) from best close; size = 2% equity risk to stop, notional cap lev x equity.
Costs: taker 0.055%/side, slippage 0.05%/fill, funding, isolated-margin liquidation (MMR 1%).
Portfolio = 8 equal sleeves (1/8 capital each, no rebalancing).
70/30 split; ONE param set across all coins, chosen on in-sample portfolio Sharpe (3x cap).
"Drawdown regime" = at signal bar close < EMA200 AND close <= 80% of 30-day (180-bar) high.
"""
import itertools, numpy as np, pandas as pd
from numba import njit
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt

ALTS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK"]
FEE, SLIP, MMR, RISK, START, DEF_FUND = 0.00055, 0.0005, 0.01, 0.02, 1000.0, 0.0001
WARM = 250

def load():
    D = {}
    for s in ALTS:
        d = pd.read_csv(f"data/{s}USDT_4h_alts.csv")
        f = pd.read_csv(f"data/{s}USDT_funding.csv")
        bar = (f.calc_time // 14_400_000) * 14_400_000  # funding event -> 4h bar containing it
        fsum = f.groupby(bar).last_funding_rate.sum()
        last = f.calc_time.max()
        fr = d.ts.map(fsum).fillna(0.0).values
        hr = pd.to_datetime(d.ts, unit="ms", utc=True).dt.hour.values
        assumed = (d.ts.values > last) & (hr % 8 == 0)
        fr = np.where(assumed, DEF_FUND, fr)
        d["fund"] = fr; d["fund_assumed"] = assumed
        d["time"] = pd.to_datetime(d.ts, unit="ms", utc=True)
        D[s] = d.reset_index(drop=True)
    return D

def atr(d, n=14):
    pc = d.close.shift()
    tr = pd.concat([d.high-d.low, (d.high-pc).abs(), (d.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean().values

def ema(x, n): return x.ewm(span=n, adjust=False).mean().values

@njit(cache=True)
def st_dir(h, l, c, a, mult):
    n = len(c); d = np.ones(n); fu = np.zeros(n); fl = np.zeros(n)
    for i in range(n):
        hl2 = (h[i]+l[i])/2; ub = hl2+mult*a[i]; lb = hl2-mult*a[i]
        if i == 0: fu[i] = ub; fl[i] = lb; continue
        fu[i] = ub if (ub < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = lb if (lb > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]
        d[i] = 1 if c[i] > fu[i-1] else (-1 if c[i] < fl[i-1] else d[i-1])
    return d

def prep(d):
    d = d.copy()
    d["atr"] = atr(d); d["ema200"] = ema(d.close, 200)
    hi30 = d.high.rolling(180).max()
    d["dd"] = ((d.close < d.ema200) & (d.close <= 0.8*hi30)).values
    return d

def edge(cond):
    prev = np.roll(cond, 1); prev[0] = False
    return cond & ~prev

def s_st(d, n, mult):
    a = pd.Series(atr(d, n)).values
    dr = st_dir(d.high.values, d.low.values, d.close.values, a, float(mult))
    cond = (dr < 0) & (d.close.values < d.ema200.values)
    return edge(cond), ~cond

def s_ema(d, f, s):
    cond = (ema(d.close, f) < ema(d.close, s)) & (d.close.values < d.ema200.values)
    return edge(cond), ~cond

def s_don(d, n, m, filt):
    ll = d.low.rolling(n).min().shift().values; hh = d.high.rolling(m).max().shift().values
    c = d.close.values
    es = c < ll
    if filt: es = es & (c < d.ema200.values)
    return es, c > hh

STRATS = {
    "ST short":  (s_st,  [(10, 2), (10, 3), (14, 4), (20, 5)]),
    "EMA short": (s_ema, [(20, 50), (20, 100), (50, 200)]),
    "Breakdown": (s_don, [(n, m, f) for n in (20, 55, 100) for m in (10, 20, 50) if m < n for f in (True, False)]),
}
STOP_K = [2, 3, 5]

@njit(cache=True)
def run(o, h, l, c, A, fr, es, xs, lev, k, i0, i1, fee, slip, mmr, risk, start):
    eq = start; pos = 0; qty = 0.0; entry = 0.0; stop = 0.0; liq = 0.0; best = 0.0; margin = 0.0
    eqe = 0.0; tp = 0.0; pend = 0; curve = np.empty(i1-i0)
    T = np.zeros((i1-i0, 5))  # entry_sig_idx, exit_idx, ret, reason(0 sig,1 stop,2 gap,3 liq,4 end), funding
    nt = 0; liqs = 0; ftot = 0.0; feet = 0.0; esig = 0; tf = 0.0
    for j in range(i1-i0):
        i = i0 + j
        if pos != 0 and fr[i] != 0.0:
            p = qty*o[i]*fr[i]; eq += p; tp += p; ftot += p; tf += p   # short receives +funding
        if pos != 0:
            if o[i] >= liq:
                eq -= margin; liqs += 1
                T[nt,0]=esig; T[nt,1]=i; T[nt,2]=(-margin+tp)/eqe; T[nt,3]=3; T[nt,4]=tf; nt+=1; pos=0
            elif o[i] >= stop:
                fill = o[i]*(1+slip); pnl = qty*(entry-fill); fe = qty*fill*fee
                eq += pnl-fe; feet += fe; tp += pnl-fe
                T[nt,0]=esig; T[nt,1]=i; T[nt,2]=tp/eqe; T[nt,3]=2; T[nt,4]=tf; nt+=1; pos=0
        if pend != 0:
            act = pend; pend = 0
            if act == 2 and pos != 0:
                fill = o[i]*(1+slip); pnl = qty*(entry-fill); fe = qty*fill*fee
                eq += pnl-fe; feet += fe; tp += pnl-fe
                T[nt,0]=esig; T[nt,1]=i; T[nt,2]=tp/eqe; T[nt,3]=0; T[nt,4]=tf; nt+=1; pos=0
            elif act == 1 and pos == 0 and eq > 0:
                px = o[i]*(1-slip); dist = k*A[i-1]
                qty = min(risk*eq/dist, lev*eq/px)
                fe = qty*px*fee; eq -= fe; feet += fe
                pos = -1; entry = px; best = px; eqe = eq+fe; tp = -fe; tf = 0.0; esig = i-1
                stop = px + dist; margin = qty*px/lev; liq = px*(1 + 1.0/lev - mmr)
        if pos != 0:
            if h[i] >= stop and stop < liq:
                fill = stop*(1+slip); pnl = qty*(entry-fill); fe = qty*fill*fee
                eq += pnl-fe; feet += fe; tp += pnl-fe
                T[nt,0]=esig; T[nt,1]=i; T[nt,2]=tp/eqe; T[nt,3]=1; T[nt,4]=tf; nt+=1; pos=0
            elif h[i] >= liq:
                eq -= margin; liqs += 1
                T[nt,0]=esig; T[nt,1]=i; T[nt,2]=(-margin+tp)/eqe; T[nt,3]=3; T[nt,4]=tf; nt+=1; pos=0
        mtm = eq + (qty*(entry-c[i]) if pos != 0 else 0.0)
        curve[j] = max(mtm, 0.0)
        if pos != 0:
            best = min(best, c[i]); stop = min(stop, best + k*A[i])
        if i < i1-1 and eq > 0:
            if pos != 0 and xs[i]: pend = 2
            elif pos == 0 and es[i]: pend = 1
    if pos != 0:
        fill = c[i1-1]*(1+slip); pnl = qty*(entry-fill); fe = qty*fill*fee
        eq += pnl-fe; feet += fe; tp += pnl-fe
        T[nt,0]=esig; T[nt,1]=i1-1; T[nt,2]=tp/eqe; T[nt,3]=4; T[nt,4]=tf; nt+=1
        curve[i1-i0-1] = max(eq, 0.0)
    return curve, T[:nt], liqs, ftot, feet, pos, entry, stop

def run_coin(d, sig, lev, k, i0, i1):
    es, xs = sig
    return run(d.open.values, d.high.values, d.low.values, d.close.values, d.atr.values, d.fund.values,
               es.astype(np.bool_), xs.astype(np.bool_), float(lev), float(k), i0, i1, FEE, SLIP, MMR, RISK, START)

def metrics(curve):
    daily = curve.resample("1D").last().dropna(); r = daily.pct_change().dropna()
    yrs = (curve.index[-1]-curve.index[0]).days/365.25; tot = curve.iloc[-1]/curve.iloc[0]-1
    return dict(total_ret_pct=100*tot, cagr_pct=100*((1+tot)**(1/yrs)-1) if tot > -1 else -100.0,
                max_dd_pct=100*(curve/curve.cummax()-1).min(),
                sharpe=float(r.mean()/r.std()*np.sqrt(365)) if r.std() > 0 else 0.0)

def tstats(tr):
    if len(tr) == 0: return dict(trades=0, win_rate_pct=np.nan, avg_trade_pct=np.nan, worst_trade_pct=np.nan, max_loss_streak=0)
    tr = tr.sort_values("exit_time"); s = m = 0
    for r in tr.ret:
        s = s+1 if r <= 0 else 0; m = max(m, s)
    return dict(trades=len(tr), win_rate_pct=100*(tr.ret > 0).mean(), avg_trade_pct=100*tr.ret.mean(),
                worst_trade_pct=100*tr.ret.min(), max_loss_streak=m)

def portfolio(D, name, p, k, lev, i0, i1, dd_only=False):
    curves, trs, liqs, fund, fees, state = [], [], 0, 0.0, 0.0, {}
    for s, d in D.items():
        es, xs = STRATS[name][0](d, *p)
        if dd_only: es = es & d.dd.values
        cv, T, lq, ft, fe, pos, ent, stp = run_coin(d, (es, xs), lev, k, i0, i1)
        curves.append(pd.Series(cv/8, index=d.time.iloc[i0:i1].values))
        t = pd.DataFrame(T, columns=["sig", "exit", "ret", "reason", "fund"]); t["coin"] = s
        if len(t):
            t["entry_time"] = d.time.values[t.sig.astype(int)]; t["exit_time"] = d.time.values[t.exit.astype(int)]
            t["dd_regime"] = d.dd.values[t.sig.astype(int)]
            t["coin_ret_pct"] = 100*t.ret
        trs.append(t); liqs += lq; fund += ft/8; fees += fe/8
        state[s] = (cv[-1]/START-1)*100
    pc = sum(curves); pc.index = pd.to_datetime(pc.index, utc=True)
    T = pd.concat(trs, ignore_index=True)
    return pc, T, liqs, fund, fees, state

def main():
    D = {s: prep(d) for s, d in load().items()}
    n = len(D["ETH"]); split = WARM + int(0.7*(n-WARM)); t = D["ETH"].time
    print(f"Data {t.iloc[WARM]} .. {t.iloc[-1]}  IS {t.iloc[WARM].date()}..{t.iloc[split-1].date()}  OOS {t.iloc[split].date()}..{t.iloc[-1].date()}")
    print("Funding assumed (no archive yet) bars:", {s: int(d.fund_assumed.sum()) for s, d in D.items()})
    tune, chosen = [], {}
    for name, (fn, grid) in STRATS.items():
        best = None
        for p, k in itertools.product(grid, STOP_K):
            pc, T, lq, *_ = portfolio(D, name, p, k, 3, WARM, split)
            m = metrics(pc); tune.append(dict(strategy=name, params=str(p), stop_k=k, **m, trades=len(T), liqs=lq))
            if best is None or m["sharpe"] > best[0]: best = (m["sharpe"], p, k)
        chosen[name] = best[1:]; print(f"{name}: chosen {best[1]} stop {best[2]}xATR  IS Sharpe {best[0]:.2f}")
    pd.DataFrame(tune).to_csv("results/alts_short_tuning_insample.csv", index=False)

    rows, curves, percoin, trades_all = [], {}, [], []
    for phase, (a, b) in {"in-sample": (WARM, split), "out-of-sample": (split, n), "full": (WARM, n)}.items():
        for name, (p, k) in chosen.items():
            for dd_only in (False, True):
                for lev in (2, 3):
                    pc, T, lq, fund, fees, st = portfolio(D, name, p, k, lev, a, b, dd_only)
                    lab = name + (" [drawdown-only]" if dd_only else "")
                    rows.append(dict(phase=phase, strategy=lab, params=str(p), stop_k=k, leverage=lev,
                                     **metrics(pc), **tstats(T), liquidations=lq,
                                     funding_received_usd=fund, fees_usd=fees))
                    if lev == 3:
                        if phase == "out-of-sample": curves[lab] = pc
                        if phase == "full":
                            T["strategy"] = lab; trades_all.append(T)
                            curves["FULL " + lab] = pc
                        if phase == "out-of-sample" and not dd_only:
                            for s, v in st.items(): percoin.append(dict(strategy=name, coin=s, oos_ret_pct=v))
        # benchmarks: buy&hold basket, short&hold 1x basket (with funding)
        bh = sum(pd.Series(d.close.values[a:b]/d.open.values[a]/8*START, index=d.time.iloc[a:b].values) for d in D.values())
        bh.index = pd.to_datetime(bh.index, utc=True)
        rows.append(dict(phase=phase, strategy="Buy & hold 8 alts", leverage=1, **metrics(bh)))
        sh = []
        for d in D.values():
            o0 = d.open.values[a]; c = d.close.values[a:b]; fcum = np.cumsum(d.fund.values[a:b]*d.open.values[a:b]/o0)
            sh.append(pd.Series(np.maximum(START/8*(1 + (o0-c)/o0 + fcum - 2*FEE), 0), index=d.time.iloc[a:b].values))
        sh = sum(sh); sh.index = pd.to_datetime(sh.index, utc=True)
        rows.append(dict(phase=phase, strategy="Short & hold 1x 8 alts", leverage=1, **metrics(sh)))
        if phase == "out-of-sample": curves["Buy & hold"] = bh; curves["Short & hold 1x"] = sh

    R = pd.DataFrame(rows); R.to_csv("results/alts_short_backtest.csv", index=False)
    pd.DataFrame(percoin).to_csv("results/alts_short_percoin_oos.csv", index=False)
    TA = pd.concat(trades_all, ignore_index=True); TA.to_csv("results/alts_short_trades_full.csv", index=False)
    pd.set_option("display.width", 260); pd.set_option("display.max_columns", 30)
    cols = ["strategy", "params", "stop_k", "leverage", "total_ret_pct", "cagr_pct", "max_dd_pct", "sharpe",
            "win_rate_pct", "trades", "avg_trade_pct", "worst_trade_pct", "max_loss_streak", "liquidations", "funding_received_usd", "fees_usd"]
    for ph in ["in-sample", "out-of-sample", "full"]:
        print(f"\n=== {ph} ==="); print(R[R.phase == ph][cols].round(2).to_string(index=False))
    print("\n=== OOS per coin (3x cap, 2% risk) ===")
    print(pd.DataFrame(percoin).pivot(index="coin", columns="strategy", values="oos_ret_pct").round(1).to_string())

    # regime analysis on full-period trades (note: IS part is fitted)
    print("\n=== Trades by regime at entry (full period, 3x) ===")
    TA["year"] = pd.to_datetime(TA.entry_time).dt.year
    out = []
    for lab, g in TA.groupby("strategy"):
        for reg, gg in [("all", g), ("year 2022", g[g.year == 2022]), ("drawdown regime", g[g.dd_regime]),
                        ("non-drawdown", g[~g.dd_regime]), ("OOS drawdown", g[g.dd_regime & (pd.to_datetime(g.entry_time, utc=True) >= t.iloc[split])])]:
            out.append(dict(strategy=lab, regime=reg, **tstats(gg), sum_trade_ret_pct=100*gg.ret.sum() if len(gg) else 0,
                            stop_gaps=int((gg.reason == 2).sum()), liqs=int((gg.reason == 3).sum())))
    RG = pd.DataFrame(out); RG.to_csv("results/alts_short_regimes.csv", index=False); print(RG.round(2).to_string(index=False))
    print("\n=== Calendar-year portfolio returns (full run, 3x) ===")
    yr = {k[5:]: v.resample("YE").last().pct_change().fillna(v.resample("YE").last().iloc[0]/v.iloc[0]-1)*100
          for k, v in curves.items() if k.startswith("FULL")}
    print(pd.DataFrame(yr).set_index(pd.DataFrame(yr).index.year).round(1).to_string())
    print("\n=== 5 worst trades (full, all strategies 3x) ===")
    print(TA.nsmallest(5, "ret")[["strategy", "coin", "entry_time", "exit_time", "coin_ret_pct", "reason"]].to_string(index=False))

    # forward returns after "drawdown" condition (the 'short into a crash' idea, strategy-free)
    fw = []
    for s, d in D.items():
        c = d.close.values; hi = d.high.values
        for lab, mask in [("drawdown state", d.dd.values), ("all bars", np.ones(n, bool))]:
            idx = np.where(mask[WARM:n-84])[0] + WARM
            for H, nm in [(18, "3d"), (42, "7d"), (84, "14d")]:
                r = c[idx+H]/c[idx]-1
                fw.append(dict(coin=s, cond=lab, h=nm, mean=r.mean()*100, median=r.median() if False else np.median(r)*100,
                               pct_up=(r > 0).mean()*100, n=len(idx)))
            if lab == "drawdown state":
                mx = np.array([hi[i+1:i+43].max()/c[i]-1 for i in idx])
                fw.append(dict(coin=s, cond=lab, h="7d max rally", mean=mx.mean()*100, median=np.median(mx)*100,
                               pct_up=(mx > 0.10).mean()*100, n=len(idx)))
    FW = pd.DataFrame(fw); FW.to_csv("results/alts_drawdown_forward.csv", index=False)
    print("\n=== Forward returns of the coin (avg over 8 coins); short profits when mean<0 ===")
    print(FW.groupby(["cond", "h"])[["mean", "median", "pct_up", "n"]].mean().round(2).to_string())
    print("(for '7d max rally', pct_up = % of cases with a >10% rally within 7 days)")

    # current signals
    print("\n=== Current state, last closed 4h bar ===")
    cur = []
    for name, (p, k) in chosen.items():
        for s, d in D.items():
            es, xs = STRATS[name][0](d, *p)
            cv, T, lq, ft, fe, pos, ent, stp = run_coin(d, (es, xs), 3, k, WARM, n)
            # run() closes at end, so re-derive open position: last trade with reason 4 == open
            openpos = len(T) and T[-1, 3] == 4
            c = d.close.iloc[-1]
            row = dict(strategy=name, coin=s, time=str(d.time.iloc[-1]), close=c, ema200=round(d.ema200.iloc[-1], 6),
                       below_ema200=bool(c < d.ema200.iloc[-1]), drawdown_state=bool(d.dd.iloc[-1]),
                       pct_from_30d_high=round(100*(c/d.high.iloc[-180:].max()-1), 1),
                       new_entry_signal_now=bool(es[-1]), in_short=bool(openpos))
            if openpos:
                i_s = int(T[-1, 0]); row["entry_signal_time"] = str(d.time.iloc[i_s]); row["entry_px"] = d.open.iloc[i_s+1]
                row["stop"] = stp; row["stop_dist_pct"] = round(100*(stp/c-1), 2); row["open_pnl_pct_of_sleeve"] = round(100*T[-1, 2], 2)
            elif es[-1]:
                stop = c + k*d.atr.iloc[-1]; row["stop"] = stop; row["stop_dist_pct"] = round(100*(stop/c-1), 2)
            cur.append(row)
    CU = pd.DataFrame(cur); CU.to_csv("results/alts_short_current.csv", index=False); print(CU.to_string(index=False))

    fig, ax = plt.subplots(2, 1, figsize=(13, 10))
    for key, cv in curves.items():
        if key.startswith("FULL"): continue
        sty = "k--" if key == "Buy & hold" else ("k:" if key.startswith("Short & hold") else "-")
        ax[0].plot(cv.index, cv.values, sty, label=key)
    ax[0].set_title(f"OUT-OF-SAMPLE {t.iloc[split].date()}..{t.iloc[-1].date()}: short-only, 8 alts, 2% risk, 3x cap, $1000")
    for key, cv in curves.items():
        if key.startswith("FULL"): ax[1].plot(cv.index, cv.values, label=key[5:])
    ax[1].axvline(t.iloc[split], color="r", ls="--", label="IS | OOS split"); ax[1].set_yscale("log")
    ax[1].axvspan(pd.Timestamp("2022-01-01", tz="UTC"), pd.Timestamp("2022-12-31", tz="UTC"), color="grey", alpha=.15)
    ax[1].set_title("Full period 2021-2026 (left of red line = fitted in-sample; grey = 2022 bear)")
    for a_ in ax: a_.legend(fontsize=8, ncol=2); a_.grid(alpha=.3); a_.set_ylabel("USD")
    plt.tight_layout(); plt.savefig("alts_short_backtest.png", dpi=110); print("\nchart: alts_short_backtest.png")

if __name__ == "__main__":
    main()
