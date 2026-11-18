"""Hyperliquid copy-trading test (public data only).
Stage 'fetch': leaderboard -> sample universe -> portfolio histories.
Stage 'sim':   select top traders by PnL BEFORE cutoff, fetch fills after cutoff,
               simulate a delayed proportional follower with costs.
Stage 'pos':   current consensus positioning of top traders.
"""
import json, os, sys, time, random, math
import requests, numpy as np, pandas as pd

API = "https://api.hyperliquid.xyz/info"
LB = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
D = "data/hl"; R = "results"
NOW = int(time.time() * 1000)
DAY = 86400_000
CUTOFF = NOW - 90 * DAY          # selection uses only data before this
SEL_START = CUTOFF - 90 * DAY    # selection window: 90 days before cutoff
TAKER = 0.00045; SLIP = 0.0005   # per side
START_EQ = 1000.0
N_SAMPLE = 1500; TOP_N = 30; MIN_AV_CUTOFF = 50_000
random.seed(42)

def post(d, tries=6):
    for i in range(tries):
        try:
            r = requests.post(API, json=d, timeout=40)
            if r.status_code == 200: return r.json()
            if r.status_code == 429: time.sleep(5 * (i + 1)); continue
            time.sleep(2)
        except Exception: time.sleep(3)
    return None

def cached(path, fn):
    if os.path.exists(path): return json.load(open(path))
    v = fn()
    if v is not None: json.dump(v, open(path, "w"))
    return v

def leaderboard():
    p = f"{D}/leaderboard.json"
    if not os.path.exists(p):
        open(p, "wb").write(requests.get(LB, timeout=120).content)
    rows = json.load(open(p))["leaderboardRows"]
    out = []
    for x in rows:
        w = dict(x["windowPerformances"])
        out.append(dict(addr=x["ethAddress"], av=float(x["accountValue"]),
            pnl_month=float(w["month"]["pnl"]), roi_month=float(w["month"]["roi"]),
            vlm_month=float(w["month"]["vlm"]), pnl_all=float(w["allTime"]["pnl"]),
            vlm_all=float(w["allTime"]["vlm"])))
    return pd.DataFrame(out)

def portfolio(a):
    return cached(f"{D}/pf/{a}.json", lambda: post({"type": "portfolio", "user": a}))

def series(pf, key, field):
    d = dict(pf).get(key)
    if not d or not d[field]: return None
    a = np.array([[t, float(v)] for t, v in d[field]])
    return a

def interp(a, t):
    if a is None or len(a) == 0: return np.nan
    if t < a[0, 0]: return 0.0 if field_is_pnl else np.nan
    return float(np.interp(t, a[:, 0], a[:, 1]))
field_is_pnl = True

def stats_at(pf):
    """PnL in selection window, AV at cutoff, trader's own pnl after cutoff (perp)."""
    global field_is_pnl
    pnl = series(pf, "allTime", "pnlHistory"); av = series(pf, "allTime", "accountValueHistory")
    if pnl is None: return None
    field_is_pnl = True
    p0, p1, p2 = interp(pnl, SEL_START), interp(pnl, CUTOFF), float(pnl[-1, 1])
    field_is_pnl = False
    av1 = interp(av, CUTOFF)
    avsel = av[(av[:, 0] >= SEL_START) & (av[:, 0] <= CUTOFF), 1] if av is not None else []
    return dict(pnl_sel=p1 - p0, pnl_after=p2 - p1, av_cut=av1,
                av_max_sel=float(np.max(avsel)) if len(avsel) else np.nan,
                first_t=float(pnl[0, 0]))

def stage_fetch():
    lb = leaderboard()
    lb.to_csv(f"{R}/hl_leaderboard_snapshot.csv", index=False)
    uni = lb[lb.vlm_all >= 5e7]
    print("leaderboard rows", len(lb), "universe vlm>=50M", len(uni))
    addrs = list(uni.addr)
    random.shuffle(addrs)
    addrs = addrs[:N_SAMPLE]
    for i, a in enumerate(addrs):
        if not os.path.exists(f"{D}/pf/{a}.json"):
            portfolio(a); time.sleep(0.9)
        if i % 100 == 0: print("pf", i, flush=True)
    json.dump(addrs, open(f"{D}/sample.json", "w"))

def build_universe():
    lb = leaderboard().set_index("addr")
    addrs = json.load(open(f"{D}/sample.json"))
    rows = []
    for a in addrs:
        p = f"{D}/pf/{a}.json"
        if not os.path.exists(p): continue
        s = stats_at(json.load(open(p)))
        if not s: continue
        s["addr"] = a; s["vlm_all"] = lb.loc[a, "vlm_all"]; s["av_now"] = lb.loc[a, "av"]
        rows.append(s)
    u = pd.DataFrame(rows)
    u["turnover"] = u.vlm_all / u.av_max_sel.clip(lower=1)
    return u

def fills_after(a):
    p = f"{D}/fills/{a}.json"
    if os.path.exists(p): return json.load(open(p))
    out, st = [], CUTOFF
    for _ in range(80):
        f = post({"type": "userFillsByTime", "user": a, "startTime": st, "endTime": NOW})
        time.sleep(0.9)
        if not f: break
        out += f
        if len(f) < 2000: break
        st = max(x["time"] for x in f) + 1
    if out and max(x["time"] for x in out) < NOW - 3 * DAY and len(out) >= 2000 * 80:
        print("WARNING truncated fills", a)
    json.dump(out, open(p, "w"))
    return out

def candles(coin):
    p = f"{D}/candles/{coin.replace('/', '_')}.json"
    def fn():
        time.sleep(0.5)
        return post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                     "startTime": CUTOFF - 2 * 3600_000, "endTime": NOW}})
    c = cached(p, fn)
    if not c: return None
    df = pd.DataFrame(c)
    df["t"] = df.t.astype(np.int64); df["o"] = df.o.astype(float); df["c"] = df.c.astype(float)
    return df.set_index("t")[["o", "c"]]

def price_at(cd, t):
    h = (t // 3600_000) * 3600_000
    if h not in cd.index:
        idx = cd.index.searchsorted(h)
        if idx >= len(cd): return float(cd.c.iloc[-1])
        return float(cd.o.iloc[idx])
    o, c = cd.loc[h, "o"], cd.loc[h, "c"]
    return float(o + (c - o) * ((t - h) / 3600_000))

def simulate(a, fills, ch_now, av_series, delay_ms):
    """Follower copies trader's position changes in perp coins, scaled by
    follower_equity / trader_account_value at the time of each change."""
    perp = [f for f in fills if not f["coin"].startswith("@") and f["time"] >= CUTOFF]
    # trader position at cutoff
    pos0 = {}
    for f in sorted(perp, key=lambda x: x["time"]):
        pos0.setdefault(f["coin"], float(f["startPosition"]))
    traded = set(pos0)
    for ap in (ch_now or {}).get("assetPositions", []):
        c = ap["position"]["coin"]
        if c not in traded: pos0[c] = float(ap["position"]["szi"])
    coins = [c for c in pos0 if pos0[c] != 0 or c in traded]
    cds = {c: candles(c) for c in coins}
    cds = {c: v for c, v in cds.items() if v is not None and len(v)}
    # events: (time, coin, trader_target_pos, trader_px)
    ev = []
    for c in cds:
        if pos0.get(c, 0): ev.append((CUTOFF, c, pos0[c], None))
    agg = {}
    for f in sorted(perp, key=lambda x: (x["time"], x["tid"])):
        if f["coin"] not in cds: continue
        sz = float(f["sz"]) * (1 if f["side"] == "B" else -1)
        new = float(f["startPosition"]) + sz
        key = (f["time"] // 60000, f["coin"])  # coalesce per minute
        agg[key] = (f["time"], f["coin"], new, float(f["px"]))
    ev += list(agg.values()); ev.sort(key=lambda x: x[0])
    def tav(t):
        v = float(np.interp(t, av_series[:, 0], av_series[:, 1]))
        return v if v > 1 else np.nan
    cash = START_EQ; pos = {c: 0.0 for c in cds}; fees = 0.0; ntrades = 0; maxlev = 0.0
    hours = np.arange((CUTOFF // 3600_000 + 1) * 3600_000, NOW, 3600_000)
    curve = []; ei = 0; bust = False
    def equity(t, use_close=True):
        e = cash
        for c, q in pos.items():
            if q: e += q * price_at(cds[c], t)
        return e
    for h in hours:
        while ei < len(ev) and ev[ei][0] + delay_ms <= h:
            t, c, tgt, tpx = ev[ei]; ei += 1
            if bust: continue
            te = t + delay_ms
            eq = equity(te)
            A = tav(t)
            if not (eq > 0) or math.isnan(A): continue
            target = tgt * eq / A
            px = tpx if (delay_ms == 0 and tpx) else price_at(cds[c], te)
            dq = target - pos[c]
            if abs(dq * px) < 1.0: continue
            fill = px * (1 + SLIP * np.sign(dq))
            cost = abs(dq * fill) * TAKER
            cash -= dq * fill + cost; fees += cost; pos[c] = target; ntrades += 1
            gross = sum(abs(q * price_at(cds[k], te)) for k, q in pos.items() if q)
            maxlev = max(maxlev, gross / max(equity(te), 1e-9))
        e = equity(h)
        if e <= 0 and not bust:
            bust = True; cash = 0.0; pos = {c: 0.0 for c in pos}; e = 0.0
        curve.append(e)
    curve = np.array(curve)
    peak = np.maximum.accumulate(curve)
    dd = float(np.min(curve / np.where(peak > 0, peak, 1) - 1)) if len(curve) else np.nan
    return dict(ret=curve[-1] / START_EQ - 1, maxdd=dd, fees=fees, trades=ntrades, bust=bust, maxlev=maxlev,
                missing_coins=len(set(pos0) - set(cds))), (hours, curve)

def stage_sim():
    u = build_universe()
    u.to_csv(f"{R}/hl_universe_sample.csv", index=False)
    elig = u[(u.av_cut >= MIN_AV_CUTOFF) & (u.first_t < SEL_START)]
    print("sample", len(u), "eligible (AV>=50k at cutoff, history before selection window)", len(elig))
    elig = elig.sort_values("pnl_sel", ascending=False)
    rows, curves = [], {}
    skipped = []
    for _, r in elig.iterrows():
        if len(rows) >= TOP_N: break
        a = r.addr
        if r.pnl_sel <= 0: break
        f = fills_after(a)
        full = len(f) == 0 or min(x["time"] for x in f) <= CUTOFF + 2 * DAY or len(f) < 2000
        if len(f) >= 10000 and not full:
            skipped.append((a, "fill history >10k, does not reach cutoff (HFT/MM)")); continue
        if not full:
            skipped.append((a, "fills do not reach cutoff")); continue
        ch = cached(f"{D}/ch/{a}.json", lambda: post({"type": "clearinghouseState", "user": a}))
        pf = json.load(open(f"{D}/pf/{a}.json"))
        av = series(pf, "allTime", "accountValueHistory")
        avm = series(pf, "month", "accountValueHistory")
        if avm is not None: av = np.vstack([av[av[:, 0] < avm[0, 0]], avm])
        own_ret = r.pnl_after / r.av_cut
        rec = dict(addr=a, pnl_sel=r.pnl_sel, av_cut=r.av_cut, trader_pnl_after=r.pnl_after,
                   trader_ret_after=own_ret, n_fills_after=len(f))
        for name, dl in [("d0", 0), ("d1h", 3600_000), ("d4h", 4 * 3600_000)]:
            res, cv = simulate(a, f, ch, av, dl)
            for k, v in res.items(): rec[f"{name}_{k}"] = v
            curves[(a, name)] = cv
        print(a[:10], {k: (round(v, 3) if isinstance(v, float) else v) for k, v in rec.items() if k != "addr"}, flush=True)
        rows.append(rec)
    res = pd.DataFrame(rows); res.to_csv(f"{R}/hl_copy_results.csv", index=False)
    pd.DataFrame(skipped, columns=["addr", "reason"]).to_csv(f"{R}/hl_skipped.csv", index=False)
    # base rates over whole eligible sample (trader's own PnL, no copying)
    base = dict(
        eligible=len(elig),
        frac_profitable_after_all=float((elig.pnl_after > 0).mean()),
        frac_profitable_after_top40=float((elig.head(TOP_N).pnl_after > 0).mean()),
        frac_profitable_after_bottom40=float((elig.sort_values("pnl_sel").head(TOP_N).pnl_after > 0).mean()),
        frac_profitable_after_top10pct=float((elig.head(max(1, len(elig)//10)).pnl_after > 0).mean()),
    )
    # rank correlation of pnl before vs after
    base["spearman_sel_vs_after_pnl"] = float(elig[["pnl_sel", "pnl_after"]].corr("spearman").iloc[0, 1])
    q = pd.qcut(elig.pnl_sel.rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
    elig = elig.assign(q=q, ret_after=elig.pnl_after / elig.av_cut)
    qt = elig.groupby("q").agg(n=("addr", "size"), med_ret_after=("ret_after", "median"),
                               frac_prof=("pnl_after", lambda s: (s > 0).mean()))
    qt.to_csv(f"{R}/hl_quintiles.csv")
    json.dump(base, open(f"{R}/hl_base_rates.json", "w"), indent=1)
    print(base); print(qt)
    # chart
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(14, 5.5))
    for name, col in [("d0", "tab:blue"), ("d1h", "tab:orange"), ("d4h", "tab:red")]:
        mats = []
        for a in res.addr:
            h, cv = curves[(a, name)]
            ax[0].plot(pd.to_datetime(h, unit="ms"), cv, color=col, alpha=0.12, lw=0.7)
            mats.append(cv)
        m = np.mean(np.vstack(mats), axis=0)
        ax[0].plot(pd.to_datetime(h, unit="ms"), m, color=col, lw=2.5, label=f"portfolio avg, delay {name[1:]}")
    ax[0].axhline(START_EQ, color="k", lw=0.8); ax[0].set_yscale("symlog", linthresh=100)
    ax[0].set_title(f"Copying top-{len(res)} Hyperliquid traders (picked by PnL before cutoff)\n$1000 each, after cutoff {pd.to_datetime(CUTOFF, unit='ms').date()}")
    ax[0].legend()
    ax[1].bar([str(i) for i in qt.index], qt.med_ret_after * 100, color="tab:gray")
    ax[1].set_title("All eligible sample: median trader return AFTER cutoff\nby PnL quintile BEFORE cutoff (5 = best)")
    ax[1].set_ylabel("%"); ax[1].axhline(0, color="k", lw=0.8)
    plt.tight_layout(); plt.savefig("hl_copy_test.png", dpi=110)

def stage_pos():
    lb = leaderboard()
    # current "top traders": month PnL leaders with real size, excluding extreme turnover (MM-like)
    t = lb[(lb.av >= 100_000) & (lb.pnl_month > 0)].copy()
    t["turn_m"] = t.vlm_month / t.av
    t = t[t.turn_m < 300].sort_values("pnl_month", ascending=False).head(100)
    rows = []
    for a in t.addr:
        ch = post({"type": "clearinghouseState", "user": a}); time.sleep(0.3)
        if not ch: continue
        av = float(ch["marginSummary"]["accountValue"])
        for ap in ch["assetPositions"]:
            p = ap["position"]; szi = float(p["szi"]); val = float(p["positionValue"])
            rows.append(dict(addr=a, av=av, coin=p["coin"], side="long" if szi > 0 else "short",
                             ntl=val * (1 if szi > 0 else -1), lev=p["leverage"]["value"]))
    df = pd.DataFrame(rows); df.to_csv(f"{R}/hl_current_positions.csv", index=False)
    g = df.groupby("coin").agg(traders=("addr", "nunique"),
            longs=("side", lambda s: (s == "long").sum()), shorts=("side", lambda s: (s == "short").sum()),
            net_ntl=("ntl", "sum"), gross_ntl=("ntl", lambda s: s.abs().sum()))
    g["net_pct"] = g.net_ntl / g.gross_ntl
    g = g.sort_values("gross_ntl", ascending=False)
    g.to_csv(f"{R}/hl_consensus.csv")
    print("traders queried", t.shape[0], "with positions", df.addr.nunique())
    print(g.head(25).to_string())

if __name__ == "__main__":
    os.makedirs(D, exist_ok=True); os.makedirs(R, exist_ok=True)
    {"fetch": stage_fetch, "sim": stage_sim, "pos": stage_pos}[sys.argv[1]]()
