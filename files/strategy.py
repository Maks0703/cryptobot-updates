"""Логика DCA, общая для бэктеста и для торговли (testnet/live)."""
from dataclasses import dataclass, field


@dataclass
class State:
    spent_usdt: float = 0.0          # сколько USDT потрачено на покупки (брутто)
    received_usdt: float = 0.0       # сколько USDT получено от продаж (нетто)
    btc: float = 0.0
    cost_basis_usdt: float = 0.0     # себестоимость текущих BTC (для средней цены)
    last_base_buy_ts: float | None = None
    last_tp_ts: float | None = None
    dip_high: float | None = None    # максимум, от которого считаем текущую просадку
    dip_levels_hit: list = field(default_factory=list)
    trades: int = 0
    dip_spent_usdt: float = 0.0     # сколько потрачено на докупки просадок
    dip_level_ts: dict = field(default_factory=dict)  # {"5": время последней докупки на уровне 5%}
    synced_from_exchange: bool = False

    @property
    def avg_entry(self):
        return self.cost_basis_usdt / self.btc if self.btc > 0 else None

    def budget_left(self, cfg):
        return max(0.0, cfg["limits"]["max_total_budget_usdt"] - self.spent_usdt)


def base_order_size(cfg):
    d = cfg["dca"]
    if d.get("base_order_usdt"):
        return float(d["base_order_usdt"])
    periods = max(1, d["horizon_days"] // d["interval_days"])
    share = d["base_share"] if cfg["dips"]["enabled"] else 1.0
    return cfg["limits"]["max_total_budget_usdt"] * share / periods


def clamp_order(cfg, st, usdt):
    """Применяет жёсткие лимиты. Возвращает 0, если ордер ставить нельзя."""
    lim = cfg["limits"]
    usdt = min(usdt, lim["max_order_usdt"], st.budget_left(cfg))
    return usdt if usdt >= lim["min_order_usdt"] else 0.0


def decide(cfg, st, now_ts, price, recent_high):
    """Возвращает список действий [("buy"|"sell", usdt_or_btc, reason)].
    now_ts — секунды, price — текущая цена, recent_high — максимум закрытий за lookback."""
    actions = []
    day = 86400
    # 1) регулярная покупка
    if st.last_base_buy_ts is None or now_ts - st.last_base_buy_ts >= cfg["dca"]["interval_days"] * day - 1:
        amt = clamp_order(cfg, st, base_order_size(cfg))
        if amt:
            actions.append(("buy", amt, "dca"))
        st.last_base_buy_ts = now_ts  # период засчитан, даже если бюджет кончился
    # 2) покупки на просадках
    if cfg["dips"]["enabled"]:
        levels = sorted(cfg["dips"]["levels"], key=lambda l: l["drawdown_pct"])
        ref = max(recent_high, price)          # скользящий максимум за lookback_days
        st.dip_high = ref
        dd = (ref - price) / ref * 100
        # цена восстановилась (просадка меньше половины первого уровня) -> уровни снова активны
        if dd < levels[0]["drawdown_pct"] / 2:
            st.dip_levels_hit = []
        reserved = sum(a[1] for a in actions if a[0] == "buy")
        base_reserved = reserved
        for lvl in levels:
            # защита от повтора: уровень не срабатывает снова, пока не прошла пауза после прошлой докупки на нём
            cool = cfg["dips"].get("level_cooldown_days", 7) * day
            last = (st.dip_level_ts or {}).get(str(lvl["drawdown_pct"]))
            if last is not None and now_ts - float(last) < cool:
                if lvl["drawdown_pct"] not in st.dip_levels_hit:
                    st.dip_levels_hit.append(lvl["drawdown_pct"])
                continue
            if dd >= lvl["drawdown_pct"] and lvl["drawdown_pct"] not in st.dip_levels_hit:
                # докупки берутся только из резерва, чтобы не съесть регулярные покупки
                reserve = cfg["limits"]["max_total_budget_usdt"] * (1 - cfg["dca"]["base_share"])
                left = min(st.budget_left(cfg) - reserved, reserve - st.dip_spent_usdt - (reserved - base_reserved))
                amt = min(lvl["order_usdt"], cfg["limits"]["max_order_usdt"], left)
                if amt >= cfg["limits"]["min_order_usdt"]:
                    actions.append(("buy", amt, f"dip-{lvl['drawdown_pct']}%"))
                    reserved += amt
                    if st.dip_level_ts is None:
                        st.dip_level_ts = {}
                    st.dip_level_ts[str(lvl["drawdown_pct"])] = now_ts
                st.dip_levels_hit.append(lvl["drawdown_pct"])
    # 3) частичная фиксация прибыли
    tp = cfg["take_profit"]
    if tp["enabled"] and st.btc > 0 and st.avg_entry:
        cooled = st.last_tp_ts is None or now_ts - st.last_tp_ts >= tp["cooldown_days"] * day
        if cooled and price >= st.avg_entry * (1 + tp["trigger_pct"] / 100):
            actions.append(("sell", st.btc * tp["sell_fraction"], "take-profit"))
            st.last_tp_ts = now_ts
    return actions


def apply_fill(st, side, price, usdt=None, btc=None, fee_pct=0.0):
    """Обновляет состояние после исполнения (симуляция или реальный fill)."""
    f = fee_pct / 100
    if side == "buy":
        got = usdt / price * (1 - f)
        st.spent_usdt += usdt
        st.btc += got
        st.cost_basis_usdt += usdt
    else:
        avg = st.avg_entry or 0
        st.btc -= btc
        st.cost_basis_usdt -= avg * btc
        st.received_usdt += btc * price * (1 - f)
    st.trades += 1
