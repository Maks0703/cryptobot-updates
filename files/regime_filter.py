"""Фильтр режима крипторынка для фьючерсного бота (только для НОВЫХ входов).

Оценка 0-100 (100 = рынок растёт, 0 = рынок падает), идея из crypto-regime-analyzer:
  * тренд BTC по дневным свечам (вес 0.6): цена выше SMA200 (+40), SMA50 выше SMA200 (+30),
    SMA200 растёт за 20 дней (+30);
  * ширина рынка (вес 0.4): доля наших 8 монет, у которых цена выше EMA200 на 4h;
  * индекс страха и жадности alternative.me (вес по умолчанию 0 = выключен).
Источник не ответил -> его вес делится между остальными. Ничего не ответило -> 50 (нейтрально).
Решение: в сильном медвежьем режиме новые ЛОНГИ запрещены (или вдвое меньше),
в сильном бычьем — новые ШОРТЫ. Открытые позиции фильтр НЕ закрывает никогда.
"""
import json, time, urllib.request

DEFAULTS = {
    "enabled": True,
    "weight_btc_trend": 0.6,
    "weight_breadth": 0.4,
    "weight_fear_greed": 0.0,      # 0 = не запрашивать индекс страха
    "block_longs_below": 20,       # оценка <= 20: новые лонги запрещены
    "halve_longs_below": 40,       # оценка < 40: новые лонги половинным размером
    "halve_shorts_above": 60,      # оценка > 60: новые шорты половинным размером
    "block_shorts_above": 80,      # оценка >= 80: новые шорты запрещены
}
DAY_MS = 86_400_000


def merged(cfg):
    out = dict(DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return out


def _sma(x, n):
    return sum(x[-n:]) / n


def btc_trend_score(closes):
    """closes: дневные закрытия BTC, от старых к новым, только закрытые дни. None, если данных мало."""
    closes = [float(c) for c in closes if c and c > 0]
    if len(closes) < 221:
        return None
    s200, s50 = _sma(closes, 200), _sma(closes, 50)
    s200_old = sum(closes[-220:-20]) / 200
    sc = 0.0
    if closes[-1] > s200: sc += 40
    if s50 > s200: sc += 30
    if s200 > s200_old: sc += 30
    return sc


def breadth_score(above_flags):
    """above_flags: список True/False (цена монеты выше EMA200). None, если монет меньше 4."""
    fl = [bool(x) for x in above_flags if x is not None]
    if len(fl) < 4:
        return None
    return 100.0 * sum(fl) / len(fl)


def combine(parts, cfg):
    """parts: {"btc_trend": score|None, "breadth": ..., "fear_greed": ...} -> оценка 0-100."""
    c = merged(cfg)
    w = {"btc_trend": c["weight_btc_trend"], "breadth": c["weight_breadth"], "fear_greed": c["weight_fear_greed"]}
    avail = {k: v for k, v in parts.items() if v is not None and w.get(k, 0) > 0}
    tw = sum(w[k] for k in avail)
    if tw <= 0:
        return 50.0
    return round(max(0.0, min(100.0, sum(avail[k] * w[k] for k in avail) / tw)), 1)


def size_mult(score, direction, cfg):
    """Множитель размера для нового входа: 1 = как обычно, 0.5 = половина, 0 = вход запрещён."""
    c = merged(cfg)
    if not c["enabled"] or score is None:
        return 1.0
    if direction == "long":
        if score <= c["block_longs_below"]: return 0.0
        if score < c["halve_longs_below"]: return 0.5
    elif direction == "short":
        if score >= c["block_shorts_above"]: return 0.0
        if score > c["halve_shorts_above"]: return 0.5
    return 1.0


def zone(score):
    if score is None: return "нет данных"
    if score < 40: return "медвежий"
    if score >= 80: return "бычий"
    return "нейтральный"


# ---------------- живые данные (на ПК Максима, без ключей) ----------------
def btc_daily_closes(cl, now_ms=None):
    """Дневные свечи BTCUSDT с Bybit (публичные данные, тот же клиент бота)."""
    now_ms = now_ms or time.time() * 1000
    r = cl.get_kline(category="linear", symbol="BTCUSDT", interval="D", limit=400)
    k = ((r or {}).get("result") or {}).get("list") or []
    rows = sorted({int(x[0]): float(x[4]) for x in k}.items())
    return [c for t, c in rows if t + DAY_MS <= now_ms]       # только закрытые дни


def fear_greed(timeout=10):
    req = urllib.request.Request("https://api.alternative.me/fng/?limit=1", headers={"User-Agent": "cryptobot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        v = float(json.loads(r.read().decode("utf-8"))["data"][0]["value"])
    return v if 0 <= v <= 100 else None


def evaluate(cl, data, cfg, log=print):
    """Считает режим рынка. Никогда не бросает исключений: при сбое источника он просто не учитывается."""
    c = merged(cfg)
    parts = {"btc_trend": None, "breadth": None, "fear_greed": None}
    if not c["enabled"]:
        return {"enabled": False, "score": None, "parts": parts, "text": "фильтр режима выключен"}
    try:
        parts["btc_trend"] = btc_trend_score(btc_daily_closes(cl))
    except Exception as e:
        log(f"режим рынка: не получил дневные свечи BTC ({str(e)[:80]}), считаю без них")
    try:
        parts["breadth"] = breadth_score([float(d.c.iloc[-1]) > float(d.es.iloc[-1]) for d in data.values()])
    except Exception as e:
        log(f"режим рынка: не посчитал ширину рынка ({str(e)[:80]})")
    if c["weight_fear_greed"] > 0:
        try:
            parts["fear_greed"] = fear_greed()
        except Exception as e:
            log(f"режим рынка: индекс страха недоступен ({str(e)[:80]}), считаю без него")
    score = combine(parts, c)
    if all(v is None for v in parts.values()):
        text = "режим рынка: данных нет, считаю нейтральным (50) — входы как обычно"
    else:
        lm, sm = size_mult(score, "long", c), size_mult(score, "short", c)
        desc = lambda m: "запрещены" if m == 0 else ("половинным размером" if m < 1 else "как обычно")
        p = ", ".join(f"{n} {'нет' if parts[k] is None else f'{parts[k]:.0f}'}"
                      for k, n in (("btc_trend", "тренд BTC"), ("breadth", "ширина"), ("fear_greed", "страх/жадность"))
                      if k != "fear_greed" or c["weight_fear_greed"] > 0)
        text = f"режим рынка {score:.0f}/100 ({zone(score)}; {p}). Новые лонги: {desc(lm)}, новые шорты: {desc(sm)}"
    return {"enabled": True, "score": score, "parts": parts, "text": text}
