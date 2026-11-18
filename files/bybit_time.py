"""Синхронизация часов с сервером Bybit (общая для bot.py и bybit_futures_bot.py).

Bybit отклоняет подписанный запрос (ErrCode 10002), если метка времени запроса
больше времени сервера более чем на 1 с или старше recv_window. Если часы компьютера
спешат, увеличение recv_window не помогает. Поэтому:
  * при запуске (и раз в час) запрашиваем публичное время сервера /v5/market/time;
  * считаем поправку = время сервера - время компьютера (по середине запроса);
  * подменяем pybit._helpers.generate_timestamp: метка = время ПК + поправка - 500 мс запаса;
  * при ошибке 10002 pybit сам повторяет запрос, а мы перед повтором пересчитываем поправку.
Ключи здесь не нужны и не используются.
"""
import json, threading, time, urllib.request

SAFETY_MS = 500            # метка чуть «в прошлом»: Bybit допускает до recv_window назад, но только 1 с вперёд
RECV_WINDOW = 20000
RESYNC_MIN_GAP_S = 30      # пересинхронизация по 10002 не чаще раза в 30 с

_state = {"offset_ms": 0.0, "synced_at": 0.0, "endpoint": "https://api-demo.bybit.com", "patched": False,
          "log": print, "last_try": 0.0}
_lock = threading.Lock()
_now = time.time           # источник времени (подменяется в тестах)


def _http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "cryptobot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_server_ms(j):
    res = (j or {}).get("result") or {}
    if res.get("timeNano"): return int(res["timeNano"]) / 1e6
    if res.get("timeSecond"): return int(res["timeSecond"]) * 1000.0
    if j.get("time"): return float(j["time"])
    raise ValueError("в ответе нет времени сервера")


def measure_offset(endpoint, samples=3, fetch=None):
    """Возвращает (поправка_мс, задержка_мс) по самому быстрому из нескольких запросов."""
    fetch = fetch or _http_get_json
    best = None
    for _ in range(samples):
        t0 = _now() * 1000
        srv = parse_server_ms(fetch(endpoint.rstrip("/") + "/v5/market/time"))
        t1 = _now() * 1000
        rtt = t1 - t0
        off = srv - (t0 + t1) / 2
        if best is None or rtt < best[1]: best = (off, rtt)
    return best


def server_timestamp():
    """Замена pybit._helpers.generate_timestamp: миллисекунды по часам сервера Bybit."""
    return int(_now() * 1000 + _state["offset_ms"] - SAFETY_MS)


def sync(endpoint=None, log=None, fetch=None, quiet=False):
    """Пересчитать поправку. Никогда не бросает исключений. Возвращает поправку или None."""
    if endpoint: _state["endpoint"] = endpoint
    if log: _state["log"] = log
    lg = _state["log"]
    with _lock:
        _state["last_try"] = _now()
        try:
            off, rtt = measure_offset(_state["endpoint"], fetch=fetch)
        except Exception as e:
            lg(f"часы: не смог узнать время сервера Bybit ({str(e)[:80]}), оставляю поправку {_state['offset_ms']/1000:+.2f} с")
            return None
        old = _state["offset_ms"]
        _state["offset_ms"], _state["synced_at"] = off, _now()
    if not quiet or abs(off - old) > 300:
        if abs(off) < 300:
            lg(f"часы: время компьютера совпадает с сервером Bybit (разница {-off/1000:+.2f} с)")
        else:
            how = "спешат" if off < 0 else "отстают"
            lg(f"часы: часы компьютера {how} на {abs(off)/1000:.2f} с относительно сервера Bybit — "
               f"исправляю автоматически (поправка {off/1000:+.2f} с)")
        if abs(off) > 600_000:
            lg("часы: разница больше 10 минут — проверьте дату и время в Windows (Параметры -> Время и язык)")
    return off


def ensure_synced(max_age_s=3600):
    if _now() - _state["synced_at"] > max_age_s:
        sync(quiet=True)


def on_10002():
    """Вызывать при ошибке 10002: быстро пересчитать поправку (не чаще раза в 30 с)."""
    if _now() - _state["last_try"] >= RESYNC_MIN_GAP_S:
        _state["log"]("часы: Bybit пишет о неверном времени запроса (10002), пересчитываю поправку")
        sync()


def install(client=None, endpoint=None, log=None, fetch=None):
    """Подключить синхронизацию к pybit. Безопасно вызывать много раз."""
    if client is not None and not endpoint:
        endpoint = str(getattr(client, "endpoint", "") or "") or None
    if log: _state["log"] = log
    try:
        from pybit import _helpers
        if _helpers.generate_timestamp is not server_timestamp:
            _helpers.generate_timestamp = server_timestamp
        try:
            from pybit import _http_manager as hm
            cls = getattr(hm, "_V5HTTPManager", None)
            orig = getattr(cls, "_handle_retryable_error", None) if cls else None
            if orig and not getattr(orig, "_cryptobot", False):
                def wrapped(self, response, error_code, error_msg, recv_window):
                    if error_code == 10002:
                        on_10002()
                    return orig(self, response, error_code, error_msg, recv_window)
                wrapped._cryptobot = True
                cls._handle_retryable_error = wrapped
        except Exception:
            pass                      # старая версия pybit: пересинхронизация из обработчиков ошибок ботов
        _state["patched"] = True
    except Exception as e:
        _state["log"](f"часы: не смог подключить поправку к pybit ({str(e)[:80]})")
    if client is not None:
        try:
            client.recv_window = max(int(getattr(client, "recv_window", 0) or 0), RECV_WINDOW)
        except Exception:
            pass
    return sync(endpoint, fetch=fetch)


def offset_ms():
    return _state["offset_ms"]
