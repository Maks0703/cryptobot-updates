"""Активная стратегия фьючерсного бота: сигналы по направлению 4h тренда (в боте — на 4h свечах).

Тренд 4h (из закрытых 4h свечей, собранных из 1h): ВВЕРХ = EMA50 > EMA200 и цена выше EMA200,
ВНИЗ = EMA50 < EMA200 и цена ниже EMA200. Против тренда бот не входит.
Типы входа (kind):
  pullback : откат к EMA20 на 1h: цена закрылась обратно выше EMA20 (лонг) / ниже EMA20 (шорт)
  rsi      : RSI14 на 1h вышел из перепроданности вверх через rsi_level (лонг) /
             из перекупленности вниз через 100-rsi_level (шорт)
  breakout : закрытие выше максимума (лонг) / ниже минимума (шорт) последних breakout_n свечей
Выход: стоп sl_atr*ATR14, тейк tp_atr*ATR14 (оба ставятся на бирже), либо по времени через max_hold_bars свечей.
Один и тот же код используется в бэктесте и в боте.
"""
import numpy as np, pandas as pd

H_MS = 3_600_000
# Выбрано по бэктесту 2021-2026 (см. отчёт): пробой 30 свечей 4h по тренду, стоп 3 ATR, тейк 4 ATR, до 30 свечей (5 дней).
DEFAULTS = {"kind": "breakout", "rsi_level": 35, "breakout_n": 30, "ema_fast_1h": 20,
            "sl_atr": 3.0, "tp_atr": 4.0, "max_hold_bars": 30,
            "trend_mode": "strict", "slope_bars": 6}
# trend_mode: strict = EMA50 и EMA200 согласны + цена по ту же сторону EMA200 (как раньше, сделок меньше);
#             slope  = только наклон EMA50 за slope_bars свечей 4h (вверх -> лонги, вниз -> шорты), сделок больше.


def merged(cfg):
    out = dict(DEFAULTS)
    if isinstance(cfg, dict): out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return out


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def to_4h(d):
    """Собрать закрытые 4h свечи (t, c) из 1h свечей (для бэктеста)."""
    g = (d.t // (4 * H_MS)) * (4 * H_MS)
    agg = d.groupby(g).agg(c=("c", "last"), n=("c", "size"))
    agg = agg[agg.n == 4]
    return pd.DataFrame({"t": agg.index.values.astype("int64"), "c": agg.c.values})


def trend_table(d4, cfg=None):
    """d4: закрытые 4h свечи (t, c). Series +1/-1/0 по времени начала 4h свечи; ema200 для ширины рынка."""
    c = merged(cfg)
    c4 = pd.Series(d4.c.values, index=d4.t.values)
    ef, es = c4.ewm(span=50, adjust=False).mean(), c4.ewm(span=200, adjust=False).mean()
    if str(c.get("trend_mode", "strict")) == "slope":
        k = max(int(c.get("slope_bars", 6)), 1); sl = ef - ef.shift(k)
        tr = pd.Series(np.where(sl > 0, 1, np.where(sl < 0, -1, 0)), index=c4.index)
        tr.iloc[:50 + k] = 0                            # прогрев EMA50
    else:
        tr = pd.Series(np.where((ef > es) & (c4 > es), 1, np.where((ef < es) & (c4 < es), -1, 0)), index=c4.index)
        tr.iloc[:200] = 0                               # прогрев EMA200
    return tr, (c4 > es) & (np.arange(len(c4)) >= 200)


def trend_4h(d, d4=None, bar_ms=H_MS, cfg=None):
    """Тренд на каждой свече из последней 4h свечи, ЗАКРЫТОЙ к закрытию этой свечи (без заглядывания вперёд)."""
    tr, _ = trend_table(to_4h(d) if d4 is None else d4, cfg)
    key = ((d.t + bar_ms) // (4 * H_MS)) * (4 * H_MS) - 4 * H_MS
    return key.map(tr).fillna(0).astype(int).values


def compute(d, cfg=None, d4=None, bar_ms=H_MS):
    """d: DataFrame t,o,h,l,c (1h, только закрытые); d4: закрытые 4h свечи (t, c) или None (собрать из 1h).
    Добавляет atr, trend, long_sig, short_sig."""
    c = merged(cfg)
    d = d.copy()
    pc = d.c.shift()
    trr = pd.concat([d.h - d.l, (d.h - pc).abs(), (d.l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = trr.ewm(alpha=1 / 14, adjust=False).mean()
    d["trend"] = trend_4h(d, d4 if d4 is not None else (d[["t", "c"]] if bar_ms == 4 * H_MS else None), bar_ms, c)
    up, dn = d.trend == 1, d.trend == -1
    if c["kind"] == "pullback":
        e = d.c.ewm(span=int(c["ema_fast_1h"]), adjust=False).mean()
        L = up & (d.c > e) & (d.c.shift() <= e.shift())
        S = dn & (d.c < e) & (d.c.shift() >= e.shift())
    elif c["kind"] == "rsi":
        r = rsi(d.c); lv = float(c["rsi_level"])
        L = up & (r >= lv) & (r.shift() < lv)
        S = dn & (r <= 100 - lv) & (r.shift() > 100 - lv)
    elif c["kind"] == "breakout":
        n = int(c["breakout_n"])
        L = up & (d.c > d.h.shift().rolling(n).max())
        S = dn & (d.c < d.l.shift().rolling(n).min())
    else:
        raise ValueError(f"неизвестный тип стратегии {c['kind']}")
    d["long_sig"], d["short_sig"] = L.fillna(False).values, S.fillna(False).values
    return d


def setup_score(x, n=None, cfg=None):
    """Сила сетапа на последней закрытой свече (для «сделок дня»): насколько цена прошла уровень пробоя,
    в ATR, по направлению тренда. None = нет тренда/данных. Возвращает (score, 'long'|'short')."""
    c = merged(cfg); n = int(n or c["breakout_n"])
    if len(x) < n + 1: return None
    tr, atr = int(x.trend.iloc[-1]), float(x.atr.iloc[-1])
    if tr == 0 or not atr > 0: return None
    hh, ll, cl = float(x.h.iloc[-n - 1:-1].max()), float(x.l.iloc[-n - 1:-1].min()), float(x.c.iloc[-1])
    return ((cl - hh) / atr, "long") if tr > 0 else ((ll - cl) / atr, "short")


def levels(entry, atr, direction, cfg=None):
    """(стоп, тейк) для входа."""
    c = merged(cfg); s = 1 if direction == "long" else -1
    return entry - s * c["sl_atr"] * atr, entry + s * c["tp_atr"] * atr
