"""Спот-бэктест альткоинов без плеча (4h, 2021-01 .. 2026-11).

Монеты: ETH SOL XRP BNB DOGE ADA AVAX LINK (USDT, спот Binance, data-api.binance.vision).
Стратегии:
  1) Сеточный бот (spot grid): геометрическая сетка из n уровней; ширина = ±w от цены
     или ширина по трейлинговому диапазону high/low за LB дней (центр = цена). Лимитные
     ордера исполняются по цене уровня (комиссия 0.1%), максимум 1 сделка на слот за свечу,
     покупка и продажа слота в одной свече запрещены (консервативно). Когда цена закрылась
     вне сетки -> пересборка по цене закрытия (ребаланс разницы по рынку, 0.1%).
     reset_down=0: при выходе вниз не пересобирать (ждать возврата), вверх - пересобирать.
     Фильтр ema: сетка работает только когда close > EMA200(4h), иначе всё в USDT.
  2) Тренд long-only: Supertrend(p,m) или EMA fast/slow. Сигнал по закрытию, вход на
     открытии следующей свечи, риск 2% капитала до стопа k*ATR(14), без плеча (позиция <= капитала),
     выход по развороту сигнала или стопу (trail=1 -> трейлинг-стоп по закрытию).
  3) Ротация: раз в неделю (понедельник 00:00 UTC) держим top-K альтов по моментуму за L дней,
     если моментум > 0 и close > EMA; опц. фильтр BTC > EMA. Иначе USDT. Комиссия 0.1% с оборота.
Бенчмарки: buy&hold каждой монеты, равновесная корзина (без ребаланса), BTC buy&hold.
Сплит 70/30: параметры выбираются только на in-sample (по Sharpe равновесного портфеля
из 8 «рукавов» по 1/8 капитала), out-of-sample — независимый прогон со стартом в кэше.
Индикаторы считаются по всей истории, но только причинно (без заглядывания вперёд).
"""
import itertools, sys, numpy as np, pandas as pd
from numba import njit
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt

ALTS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK"]
FEE = 0.001
BPY = 6 * 365  # 4h bars per year

# ---------------- data ----------------
def load():
    dfs = {}
    for s in ALTS + ["BTC"]:
        try:
            d = pd.read_csv(f"data/{s}USDT_4h_alts.csv")
        except FileNotFoundError:
            print("skip", s); continue
        dfs[s] = d.set_index("ts")
    idx = sorted(set.intersection(*[set(d.index) for d in dfs.values()]))
    for s in dfs: dfs[s] = dfs[s].loc[idx]
    t = pd.to_datetime(pd.Index(idx), unit="ms", utc=True)
    return dfs, t

# ---------------- indicators ----------------
def atr(d, n=14):
    pc = d.close.shift()
    tr = pd.concat([d.high-d.low, (d.high-pc).abs(), (d.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean().values

def ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False, min_periods=n).mean().values

@njit(cache=True)
def st_dir(h, l, c, a, mult):
    n = len(c); d = np.zeros(n); fu = np.zeros(n); fl = np.zeros(n)
    for i in range(n):
        hl2 = (h[i]+l[i])/2
        if np.isnan(a[i]):
            continue
        ub = hl2 + mult*a[i]; lb = hl2 - mult*a[i]
        if i == 0 or d[i-1] == 0:
            fu[i] = ub; fl[i] = lb; d[i] = 1 if c[i] > hl2 else -1; continue
        fu[i] = ub if (ub < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = lb if (lb > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]
        d[i] = 1 if c[i] > fu[i-1] else (-1 if c[i] < fl[i-1] else d[i-1])
    return d

# ---------------- grid ----------------
@njit(cache=True)
def sim_grid(h, l, c, emaf, hw, start, end, n, use_filt, reset_down, fm, ft):
    """hw[i] = half-width (fraction) of range to use if (re)built at close i (nan -> can't build)."""
    eq = np.zeros(end-start); cash = np.zeros(n); qty = np.zeros(n); lv = np.zeros(n+1)
    free = 1.0; active = False; trades = 0
    for i in range(start, end):
        P = c[i]
        if active:
            for k in range(n):
                if qty[k] == 0.0 and cash[k] > 0 and l[i] <= lv[k]:
                    qty[k] = cash[k] / (lv[k]*(1+fm)); cash[k] = 0.0; trades += 1
                elif qty[k] > 0 and h[i] >= lv[k+1]:
                    cash[k] = qty[k]*lv[k+1]*(1-fm); qty[k] = 0.0; trades += 1
        want = (use_filt == 0) or (not np.isnan(emaf[i]) and P > emaf[i])
        if np.isnan(hw[i]): want = False
        E = free + cash.sum() + qty.sum()*P
        rebuild = False
        if active and not want:
            Q = qty.sum()
            if Q > 0: trades += 1
            free = cash.sum() + Q*P*(1-ft); cash[:] = 0; qty[:] = 0; active = False
        elif active and (P > lv[n] or (P < lv[0] and reset_down == 1)):
            rebuild = True
        elif (not active) and want:
            rebuild = True
        if rebuild:
            w = hw[i]; lo = P*(1-w); hi = P*(1+w)
            for k in range(n+1): lv[k] = lo*(hi/lo)**(k/n)
            m = 0
            for k in range(n):
                if lv[k+1] > P: m += 1
            Q = qty.sum(); target_val = m*E/n
            notional = abs(target_val - Q*P)
            if notional > 1e-12: trades += 1
            E2 = E - notional*ft; q = E2/n
            for k in range(n):
                if lv[k+1] > P: qty[k] = q/P; cash[k] = 0.0
                else: qty[k] = 0.0; cash[k] = q
            free = 0.0; active = True
        eq[i-start] = free + cash.sum() + qty.sum()*P
    return eq, trades

# ---------------- trend ----------------
@njit(cache=True)
def sim_trend(o, h, l, c, up, a, start, end, k, trail, risk, f):
    """up[i]: 1 trend up / -1 down / 0 unknown at close i."""
    eq = np.zeros(end-start); cash = 1.0; qty = 0.0; stop = 0.0; trades = 0
    pending = 0  # 1 enter at next open, -1 exit at next open
    pend_stopdist = 0.0; blocked = False
    for i in range(start, end):
        # execute pending at open
        if pending == 1 and qty == 0.0:
            px = o[i]; E = cash
            q = risk*E/pend_stopdist
            q = min(q, E/(px*(1+f)))
            qty = q; cash -= q*px*(1+f); stop = px - pend_stopdist; trades += 1
        elif pending == -1 and qty > 0:
            cash += qty*o[i]*(1-f); qty = 0.0; trades += 1
        pending = 0
        # stop
        if qty > 0 and l[i] <= stop:
            px = min(o[i], stop)
            cash += qty*px*(1-f); qty = 0.0; trades += 1; blocked = True
        # signals at close
        if up[i] < 0: blocked = False
        if qty > 0:
            if up[i] < 0: pending = -1
            elif trail == 1 and not np.isnan(a[i]):
                ns = c[i] - k*a[i]
                if ns > stop: stop = ns
        else:
            if up[i] > 0 and (not blocked) and not np.isnan(a[i]) and i < end-1:
                pending = 1; pend_stopdist = k*a[i]
        eq[i-start] = cash + qty*c[i]
    return eq, trades

# ---------------- metrics ----------------
def metrics(eq, t):
    eq = np.asarray(eq, float); eq = eq/eq[0]
    r = np.diff(eq)/eq[:-1]
    yrs = (t[-1]-t[0]).total_seconds()/(365.25*86400)
    tot = eq[-1]-1; cagr = eq[-1]**(1/yrs)-1 if eq[-1] > 0 else -1
    dd = (eq/np.maximum.accumulate(eq)-1).min()
    sh = r.mean()/r.std()*np.sqrt(BPY) if r.std() > 0 else 0.0
    s = pd.Series(eq, index=t)
    me = s.resample("ME").last(); me = pd.concat([pd.Series([s.iloc[0]], index=[t[0]]), me])
    mr = me.pct_change().dropna()
    return dict(total=tot, cagr=cagr, maxdd=dd, sharpe=sh, avg_month=mr.mean(), pos_months=(mr > 0).mean())

def fmt(m):
    return f"tot {m['total']*100:+7.1f}% CAGR {m['cagr']*100:+6.1f}% DD {m['maxdd']*100:6.1f}% Sh {m['sharpe']:5.2f} mon {m['avg_month']*100:+5.2f}% pos {m['pos_months']*100:4.0f}%"

# ---------------- main ----------------
def main(fee=FEE, tag=""):
    dfs, t = load()
    alts = [s for s in ALTS if s in dfs]
    N = len(t); S = int(N*0.7)
    periods = {"IS": (0, S), "OOS": (S, N)}
    print(f"bars {N}, IS {t[0]:%Y-%m-%d}..{t[S-1]:%Y-%m-%d}, OOS {t[S]:%Y-%m-%d}..{t[-1]:%Y-%m-%d}")
    A = {}
    for s in dfs:
        d = dfs[s]
        A[s] = dict(o=d.open.values, h=d.high.values, l=d.low.values, c=d.close.values,
                    atr14=atr(d, 14), ema200=ema(d.close.values, 200), d=d)
    rows = []; curves = {}

    def add(strategy, params, coin, per, eq, trades):
        a, b = periods[per]; m = metrics(eq, t[a:b])
        rows.append(dict(strategy=strategy, params=params, coin=coin, period=per, trades=trades, **m))
        return m

    # ---- benchmarks
    for per, (a, b) in periods.items():
        basket = np.zeros(b-a)
        for s in alts + ["BTC"]:
            c = A[s]["c"]; e = c[a:b]/c[a]*(1-fee)
            add("buy&hold", "", s, per, e, 1)
            if s != "BTC": basket += e/len(alts)
        add("buy&hold", "equal-weight basket", "ALL", per, basket, len(alts))
        curves[("EW basket B&H", per)] = basket
        curves[("BTC B&H", per)] = A["BTC"]["c"][a:b]/A["BTC"]["c"][a]*(1-fee)

    # ---- grid
    grid_cfgs = []
    for n in (10, 15, 20):
        for filt in (0, 1):
            for rd in (1, 0):
                for w in (0.10, 0.15, 0.20, 0.30): grid_cfgs.append(("pct", w, n, filt, rd))
                for lb in (14, 30, 60): grid_cfgs.append(("trail", lb, n, filt, rd))
    hw_cache = {}
    for s in alts:
        d = A[s]["d"]
        for lb in (14, 30, 60):
            hi = d.high.rolling(lb*6).max().values; lo = d.low.rolling(lb*6).min().values
            hw_cache[(s, lb)] = np.clip((hi-lo)/(2*d.close.values), 0.05, 0.5)
    def grid_label(cfg):
        mode, x, n, filt, rd = cfg
        r = f"±{int(x*100)}%" if mode == "pct" else f"trail{x}d-range"
        return f"{r} n={n} filt={'EMA200' if filt else 'none'} reset_down={rd}"
    def run_grid(cfg, per, record=True):
        mode, x, n, filt, rd = cfg; a, b = periods[per]; port = np.zeros(b-a); tr = 0; per_coin = {}
        for s in alts:
            X = A[s]
            hw = np.full(N, x) if mode == "pct" else hw_cache[(s, x)]
            e, k = sim_grid(X["h"], X["l"], X["c"], X["ema200"], hw, a, b, n, filt, rd, fee, fee)
            port += e/len(alts); tr += k; per_coin[s] = (e, k)
        return port, tr, per_coin

    # ---- trend
    trend_cfgs = []
    for sig in [("ST", 10, 2), ("ST", 10, 3), ("ST", 14, 3), ("ST", 20, 3), ("ST", 20, 4),
                ("EMA", 20, 50), ("EMA", 50, 200), ("EMA", 100, 400)]:
        for k in (2, 3, 4):
            for trail in (0, 1): trend_cfgs.append((sig, k, trail))
    sig_cache = {}
    for s in alts:
        X = A[s]; d = X["d"]
        for sig in set(c[0] for c in trend_cfgs):
            if sig[0] == "ST":
                u = st_dir(X["h"], X["l"], X["c"], atr(d, sig[1]), float(sig[2]))
            else:
                ef, es = ema(X["c"], sig[1]), ema(X["c"], sig[2])
                u = np.where(np.isnan(es), 0.0, np.sign(ef-es))
            sig_cache[(s, sig)] = u
    def trend_label(cfg):
        sig, k, trail = cfg
        nm = f"Supertrend({sig[1]},{sig[2]})" if sig[0] == "ST" else f"EMA{sig[1]}/{sig[2]}"
        return f"{nm} stop={k}ATR {'trailing' if trail else 'fixed'} risk2%"
    def run_trend(cfg, per):
        sig, k, trail = cfg; a, b = periods[per]; port = np.zeros(b-a); tr = 0; per_coin = {}
        for s in alts:
            X = A[s]
            e, kk = sim_trend(X["o"], X["h"], X["l"], X["c"], sig_cache[(s, sig)], X["atr14"], a, b, float(k), trail, 0.02, fee)
            port += e/len(alts); tr += kk; per_coin[s] = (e, kk)
        return port, tr, per_coin

    # ---- rotation
    C = np.column_stack([A[s]["c"] for s in alts]); O = np.column_stack([A[s]["o"] for s in alts])
    btc_c = A["BTC"]["c"]
    ema_cache = {n: np.column_stack([ema(A[s]["c"], n) for s in alts]) for n in (200, 1200)}
    btc_ema = {n: ema(btc_c, n) for n in (200, 1200)}
    monday = np.array([(tt.day_of_week == 0 and tt.hour == 0) for tt in t])
    rot_cfgs = [(K, L, en, bf) for K in (1, 2, 3) for L in (7, 14, 30, 60) for en in (200, 1200) for bf in (0, 1)]
    def rot_label(cfg):
        K, L, en, bf = cfg
        return f"top{K} mom{L}d EMA{'200(4h)' if en == 200 else '200(1d)'} btcfilter={bf} weekly"
    def run_rot(cfg, per):
        K, L, en, bf = cfg; a, b = periods[per]; Lb = L*6
        E = np.zeros(b-a); hold = np.zeros(len(alts)); cash = 1.0; trades = 0
        for i in range(a, b):
            if monday[i] and i > a:  # decided on close i-1, executed at open i
                j = i-1; px = O[i]
                val = cash + (hold*px).sum()
                w = np.zeros(len(alts))
                if j-Lb >= 0:
                    mom = C[j]/C[j-Lb]-1; em = ema_cache[en][j]
                    ok = (mom > 0) & (C[j] > em) & ~np.isnan(em)
                    if bf and not (btc_c[j] > btc_ema[en][j]): ok[:] = False
                    cand = [x for x in np.argsort(-mom) if ok[x]][:K]
                    for x in cand: w[x] = 1.0/K
                tgt = w*val/px; dq = tgt-hold
                turnover = (np.abs(dq)*px).sum()
                trades += int((np.abs(dq*px) > 1e-9*val).sum())
                hold = tgt; cash = val - (tgt*px).sum() - turnover*fee
            E[i-a] = cash + (hold*C[i]).sum()
        return E, trades, {}

    families = [("grid", grid_cfgs, grid_label, run_grid), ("trend", trend_cfgs, trend_label, run_trend),
                ("rotation", rot_cfgs, rot_label, run_rot)]
    best = {}
    for fam, cfgs, lab, run in families:
        res = []
        for cfg in cfgs:
            out = {}
            for per in periods:
                port, tr, pc = run(cfg, per)
                out[per] = add(fam, lab(cfg), "ALL", per, port, tr)
            res.append((cfg, out))
        res.sort(key=lambda x: -x[1]["IS"]["sharpe"])
        cfg = res[0][0]; best[fam] = cfg
        oos_rank = sorted(res, key=lambda x: -x[1]["OOS"]["sharpe"]).index(res[0]) + 1
        oos_best = max(res, key=lambda x: x[1]["OOS"]["sharpe"])
        print(f"\n== {fam}: {len(cfgs)} configs. Best IS: {lab(cfg)} (OOS rank {oos_rank}/{len(cfgs)})")
        for per in periods:
            port, tr, pc = run(cfg, per)
            print(f"  {per:3s} {fmt(res[0][1][per])} trades {tr}")
            curves[(f"{fam}: {lab(cfg)}", per)] = port
            for s, (e, k) in pc.items():
                m = add(fam + " [best-IS cfg]", lab(cfg), s, per, e, k)
                if per == "OOS": print(f"     {s:5s} {fmt(m)} trades {k}")
        print(f"  (for reference only, best OOS cfg: {lab(oos_best[0])} -> {fmt(oos_best[1]['OOS'])})")
        meds = np.median([r[1]["OOS"]["total"] for r in res])
        print(f"  median OOS total over all {len(cfgs)} configs: {meds*100:+.1f}%")

    print("\n== benchmarks")
    for r in rows:
        if r["strategy"] == "buy&hold":
            print(f"  {r['period']:3s} {r['coin']:5s} {fmt(r)}")

    pd.DataFrame(rows).to_csv(f"results/alts_backtest{tag}.csv", index=False)
    if tag: return
    fig, axes = plt.subplots(2, 1, figsize=(13, 10))
    for ax, per in zip(axes, periods):
        a, b = periods[per]
        for (name, p), e in curves.items():
            if p != per: continue
            ax.plot(t[a:b], e/e[0], label=name, lw=1.6 if "B&H" not in name else 1.0,
                    ls="--" if "B&H" in name else "-")
        ax.set_yscale("log"); ax.grid(alpha=.3); ax.legend(fontsize=8)
        ax.set_title(f"{'In-sample' if per == 'IS' else 'OUT-OF-SAMPLE'} {t[a]:%Y-%m-%d}..{t[b-1]:%Y-%m-%d} (spot, no leverage, fee {fee*100:.2f}%/side)")
    plt.tight_layout(); plt.savefig("alts_backtest.png", dpi=110)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--kraken-fee":
        main(fee=0.0026, tag="_kraken_fee")
    else:
        main()
