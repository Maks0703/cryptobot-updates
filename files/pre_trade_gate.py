"""Проверка перед каждой сделкой (идея из pre-trade-discipline-gate).

Вызывается ботом прямо перед отправкой ордера (и для обычного сигнала, и для «сделки дня»).
Не меняет выбор Максима (профиль, риск, число позиций, сделку дня) — только не пускает явно
плохие ордера. Все пороги в config.yaml -> futures_protection -> pre_trade_gate.

Правила (block = ордер не отправляется, причина пишется в окно и в журнал):
  1. sl_tp        стоп есть и с правильной стороны; в активном режиме есть и тейк, тоже с правильной стороны
  2. min_rr       тейк/стоп (прибыль к риску) не меньше min_rr (по умолчанию 1.2)
  3. risk         убыток на стопе после округления объёма <= risk_pct капитала + допуск
  4. total_risk   суммарный риск всех открытых позиций + новой <= лимита (агрессивный 40%, обычный 6%)
  5. duplicate    по монете уже есть позиция или висит ордер
  6. same_dir     позиций в одну сторону не больше max_same_direction (0 = без лимита, только запись в окно)
  7. spread       спред по стакану > max_spread_pct (0.3%) -> монету пропускаем
  8. news         новостной фильтр запрещает входы
  9. cooldown     по монете был стоп-аут меньше cooldown_bars свечей назад
"""
import math

DEFAULTS = {
    "enabled": True,
    "min_rr": 1.2,
    "risk_tolerance_pct": 10.0,          # допуск к риску на сделку, % от него (10% от 10% = 11%)
    "fee_pct": 0.055,                    # тейкер Bybit на сторону, для оценки убытка на стопе
    "total_risk_cap_pct_normal": 6.0,
    "total_risk_cap_pct_aggressive": 40.0,
    "max_same_direction": 0,             # 0 = не ограничивать, только писать в окно
    "max_spread_pct": 0.3,
    "cooldown_bars": 2,
}
BAR_MS = 14_400_000


def merged(cfg):
    out = dict(DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return out


def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def position_risk(p):
    """Сколько USDT потеряем, если сработает стоп позиции (0, если стоп уже в плюсе)."""
    e, st, q = _num(p.get("entry")), _num(p.get("stop")), _num(p.get("qty"))
    if not e or st is None or not q: return 0.0
    loss = (e - st) * q if p.get("dir") == "long" else (st - e) * q
    return max(0.0, loss)


def check(order, ctx, cfg=None):
    """order: coin, direction, price, qty, stop, tp (или None), equity, risk_pct, require_tp
    ctx: profile, positions (бот: coin -> {dir, entry, stop, qty}), exch_symbols (set), open_order_symbols (set|None),
         news_block (bool), stopouts (coin -> ms), now_ms, bid, ask, symbol
    Возвращает (ok: bool, причины отказа: list[str], заметки: list[str])."""
    c = merged(cfg)
    if not c["enabled"]:
        return True, [], ["проверка перед сделкой выключена"]
    bad, notes = [], []
    coin, d = order.get("coin"), order.get("direction")
    px, qty, sl, tp = _num(order.get("price")), _num(order.get("qty")), _num(order.get("stop")), _num(order.get("tp"))
    eq, rp = _num(order.get("equity")), _num(order.get("risk_pct"))
    sg = 1 if d == "long" else -1 if d == "short" else 0
    if not sg or not px or px <= 0 or not qty or qty <= 0 or not eq or eq <= 0:
        return False, ["неполные данные ордера (направление/цена/объём/капитал)"], notes

    # 1. стоп и тейк
    if sl is None or sl <= 0 or (sl - px) * sg >= 0:
        bad.append("нет стоп-лосса или он не с той стороны цены")
    if order.get("require_tp", True):
        if tp is None or tp <= 0 or (tp - px) * sg <= 0:
            bad.append("нет тейк-профита или он не с той стороны цены")
        # 2. прибыль/риск
        elif sl is not None and (sl - px) * sg < 0:
            rr = abs(tp - px) / abs(px - sl)
            if rr < float(c["min_rr"]) - 1e-9:
                bad.append(f"прибыль/риск {rr:.2f} меньше {float(c['min_rr']):g}")
    # 3. риск сделки после округления
    loss = None
    if sl is not None and (sl - px) * sg < 0:
        loss = qty * abs(px - sl)   # как в расчёте размера бота (без комиссий)
        fees = 2 * qty * px * float(c["fee_pct"]) / 100
        notes.append(f"план: убыток на стопе {loss:.2f} USDT ({100*loss/eq:.2f}% капитала) + комиссии ~{fees:.2f} USDT")
        if rp is not None:
            lim = eq * rp / 100 * (1 + float(c["risk_tolerance_pct"]) / 100)
            if loss > lim:
                bad.append(f"убыток на стопе {loss:.2f} USDT = {100*loss/eq:.2f}% капитала больше лимита {rp:g}% (+допуск)")
    # 4. суммарный риск
    poss = ctx.get("positions") or {}
    cap = float(c["total_risk_cap_pct_aggressive"] if ctx.get("profile") == "aggressive" else c["total_risk_cap_pct_normal"])
    open_risk = sum(position_risk(p) for p in poss.values())
    if loss is not None and open_risk + loss > eq * cap / 100 + 1e-9:
        bad.append(f"суммарный риск открытых позиций {100*open_risk/eq:.1f}% + новая {100*loss/eq:.1f}% больше лимита {cap:g}%")
    # 5. дубль
    sym = ctx.get("symbol") or f"{coin}USDT"
    if coin in poss or sym in (ctx.get("exch_symbols") or set()):
        bad.append("по монете уже есть позиция")
    oo = ctx.get("open_order_symbols")
    if oo is None:
        notes.append("открытые ордера не проверены (биржа не ответила)")
    elif sym in oo:
        bad.append("по монете уже висит ордер")
    # 6. одна сторона
    same = sum(1 for p in poss.values() if p.get("dir") == d)
    mx = int(c["max_same_direction"] or 0)
    if mx > 0 and same >= mx:
        bad.append(f"уже {same} позиций в {('лонг' if d == 'long' else 'шорт')} (лимит {mx})")
    elif same >= 3:
        notes.append(f"внимание: это будет {same + 1}-я позиция в {('лонг' if d == 'long' else 'шорт')}, монеты сильно связаны между собой")
    # 7. спред
    bid, ask = _num(ctx.get("bid")), _num(ctx.get("ask"))
    if bid and ask and ask >= bid > 0:
        spr = 100 * (ask - bid) / ((ask + bid) / 2)
        if spr > float(c["max_spread_pct"]):
            bad.append(f"спред {spr:.2f}% больше {float(c['max_spread_pct']):g}% — мало ликвидности")
    else:
        notes.append("спред не проверен (нет цен покупки/продажи)")
    # 8. новости
    if ctx.get("news_block"):
        bad.append("новостной фильтр запрещает входы")
    # 9. пауза после стопа
    so = (ctx.get("stopouts") or {}).get(coin)
    now = _num(ctx.get("now_ms"))
    if so and now is not None and now - float(so) < int(c["cooldown_bars"]) * BAR_MS:
        hrs = (now - float(so)) / 3_600_000
        bad.append(f"стоп-аут по монете {hrs:.1f} ч назад, пауза {int(c['cooldown_bars'])} свечи 4h")
    return not bad, bad, notes
