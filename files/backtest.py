"""Бэктест: обычный DCA vs DCA с докупками на просадках vs купить и держать."""
import copy, json
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from strategy import State, decide, apply_fill


def load(tf):
    df = pd.read_csv(f"data/BTCUSDT_{tf}.csv")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.reset_index(drop=True)


def simulate(cfg, df, tf):
    fee = cfg["fees"]["taker_pct"]
    budget = cfg["limits"]["max_total_budget_usdt"]
    bars_per_day = {"1d": 1, "4h": 6}[tf]
    lb = cfg["dips"]["lookback_days"] * bars_per_day
    roll_high = df.close.rolling(lb, min_periods=1).max()
    # решение принимается на закрытии свечи, исполнение по цене закрытия
    st, eq = State(), []
    for i, row in df.iterrows():
        ts = row.ts / 1000 + 86400 / bars_per_day  # момент закрытия свечи
        for side, amt, why in decide(cfg, st, ts, row.close, roll_high[i]):
            if why.startswith("dip"):
                st.dip_spent_usdt += amt
            if side == "buy":
                apply_fill(st, "buy", row.close, usdt=amt, fee_pct=fee)
            else:
                apply_fill(st, "sell", row.close, btc=amt, fee_pct=fee)
        cash = budget - st.spent_usdt + st.received_usdt
        eq.append(cash + st.btc * row.close)
    return st, pd.Series(eq, index=df.time)


def buy_hold(cfg, df):
    fee = cfg["fees"]["taker_pct"] / 100
    budget = cfg["limits"]["max_total_budget_usdt"]
    btc = budget / df.close.iloc[0] * (1 - fee)
    st = State(spent_usdt=budget, btc=btc, cost_basis_usdt=budget, trades=1)
    return st, pd.Series(btc * df.close.values, index=df.time)


def max_dd(eq):
    return float(((eq - eq.cummax()) / eq.cummax()).min() * 100)


def metrics(name, cfg, st, eq):
    budget = cfg["limits"]["max_total_budget_usdt"]
    final = float(eq.iloc[-1])
    return {
        "strategy": name,
        "invested_usdt": round(st.spent_usdt, 2),
        "cash_left_usdt": round(budget - st.spent_usdt + st.received_usdt, 2),
        "btc_held": round(st.btc, 6),
        "final_value_usdt": round(final, 2),
        "return_on_invested_pct": round((st.btc * float(eq.attrs["last_price"]) + st.received_usdt - st.spent_usdt) / st.spent_usdt * 100, 2) if st.spent_usdt else 0.0,
        "return_on_budget_pct": round((final / budget - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd(eq), 2),
        "trades": st.trades,
        "avg_entry_price": round(st.cost_basis_usdt / st.btc, 2) if st.btc else None,
    }


def run(cfg, tf="1d", chart="backtest.png", with_tp=True):
    df = load(tf)
    variants = {}
    c_plain = copy.deepcopy(cfg); c_plain["dips"]["enabled"] = False; c_plain["take_profit"]["enabled"] = False
    variants["DCA (обычный)"] = simulate(c_plain, df, tf)
    c_dip = copy.deepcopy(cfg); c_dip["dips"]["enabled"] = True; c_dip["take_profit"]["enabled"] = False
    variants["DCA + просадки"] = simulate(c_dip, df, tf)
    if with_tp:
        c_tp = copy.deepcopy(c_dip); c_tp["take_profit"]["enabled"] = True
        variants["DCA + просадки + TP"] = simulate(c_tp, df, tf)
    variants["Купить и держать"] = buy_hold(cfg, df)
    for _, (_, eq) in variants.items():
        eq.attrs["last_price"] = float(df.close.iloc[-1])
    rows = [metrics(k, cfg, st, eq) for k, (st, eq) in variants.items()]

    fig, (axp, ax) = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [1, 1.6]})
    axp.plot(df.time, df.close, color="orange", lw=1.2)
    axp.set_ylabel("Цена BTC, USDT"); axp.grid(alpha=0.3)
    axp.set_title(f"BTC/USDT спот, свечи {tf}, бюджет {cfg['limits']['max_total_budget_usdt']} USDT, "
                  f"комиссия {cfg['fees']['taker_pct']}%  ({df.time.iloc[0]:%Y-%m-%d} — {df.time.iloc[-1]:%Y-%m-%d})")
    styles = {"DCA + просадки + TP": dict(ls="--", lw=1.2)}
    for k, (_, eq) in variants.items():
        ax.plot(eq.index, eq.values, label=k, **styles.get(k, dict(lw=1.6)))
    ax.axhline(cfg["limits"]["max_total_budget_usdt"], color="gray", ls=":", lw=1)
    ax.set_ylabel("Портфель (свободные USDT + BTC), USDT")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3); fig.tight_layout(); fig.savefig(chart, dpi=120)
    return rows, df
