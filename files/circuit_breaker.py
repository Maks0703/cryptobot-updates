"""Аварийный стоп по просадке для фьючерсного бота (идея из drawdown-circuit-breaker).

Считает капитал счёта каждый прогон и запоминает максимум (пик) в state/circuit_breaker.json
(эта папка не попадает в обновления и в хранилище).

Правила (значения по умолчанию в DEFAULTS, меняются в config.yaml -> futures_protection -> circuit_breaker):
  * ДНЕВНОЙ стоп: капитал упал на daily_limit_pct% от начала текущих суток UTC
      -> новые позиции не открываются до следующих суток UTC (сброс автоматически в 00:00 UTC,
         в Минске это 03:00).
  * СТОП ОТ ПИКА: капитал упал на peak_limit_pct% от максимума
      -> новые позиции не открываются, пока вы не сбросите стоп вручную:
         положите в папку бота пустой файл «сброс_стопа.txt» (бот его удалит) или удалите
         state/circuit_breaker.json. После сброса пиком считается текущий капитал.
  * ЗАКРЫТЬ ВСЁ (по умолчанию ВЫКЛЮЧЕНО, close_all_enabled: false): при просадке close_all_pct%
    от пика бот один раз закрывает все свои позиции по рынку.
  Чтобы один ошибочный ответ биржи не включил долгий стоп, стоп от пика и «закрыть всё»
  запоминаются только после confirm_runs прогонов подряд. До этого новые входы просто
  пропускаются в этом прогоне. Открытые позиции стоп НЕ трогает (кроме «закрыть всё»).
"""
import json, math, os
from datetime import datetime, timezone

DEFAULTS = {
    "enabled": True,
    "daily_limit_pct": 5.0,
    "peak_limit_pct": 20.0,
    "close_all_enabled": False,
    "close_all_pct": 25.0,
    "confirm_runs": 2,
}
RESET_FILE_NAME = "сброс_стопа.txt"


def merged(cfg):
    out = dict(DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return out


class Breaker:
    def __init__(self, state_path, cfg=None, reset_path=None):
        self.path = state_path
        self.reset_path = reset_path
        self.c = merged(cfg)

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                s = json.load(fh)
            if not isinstance(s, dict) or not (float(s.get("peak", 0)) > 0):
                raise ValueError("нет пика")
            return s, None
        except FileNotFoundError:
            return None, None
        except Exception as e:
            try: os.replace(self.path, self.path + ".bad")
            except OSError: pass
            return None, f"файл {os.path.basename(self.path)} испорчен ({e}), начинаю отсчёт заново"

    def _save(self, s):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def update(self, equity, now=None):
        """Возвращает dict: block_new (bool), close_all (bool), messages (list[str]), dd_day, dd_peak, peak."""
        now = now or datetime.now(timezone.utc)
        c = self.c
        res = {"block_new": False, "close_all": False, "messages": [], "dd_day": 0.0, "dd_peak": 0.0, "peak": None}
        if not c["enabled"]:
            res["messages"].append("аварийный стоп по просадке выключен")
            return res
        try:
            equity = float(equity)
        except (TypeError, ValueError):
            equity = float("nan")
        if not math.isfinite(equity) or equity <= 0:
            res["block_new"] = True
            res["messages"].append(f"аварийный стоп: странный капитал ({equity}), новые входы в этом прогоне пропускаю")
            return res
        today = now.strftime("%Y-%m-%d")
        s, warn = self._load()
        if warn: res["messages"].append("аварийный стоп: " + warn)
        if self.reset_path and os.path.exists(self.reset_path):
            try: os.remove(self.reset_path)
            except OSError: pass
            s = None
            res["messages"].append(f"аварийный стоп СБРОШЕН вручную (файл {RESET_FILE_NAME}). Пик = {equity:.2f} USDT")
        if s is None:
            s = {"peak": equity, "peak_time": now.isoformat(timespec="minutes"), "day": today, "day_start": equity,
                 "daily_tripped": False, "peak_tripped": False, "peak_tripped_at": None, "closed_all_at": None,
                 "peak_bad_runs": 0, "close_bad_runs": 0}
        if s.get("day") != today:
            if s.get("daily_tripped"):
                res["messages"].append("новые сутки UTC: дневной стоп снят")
            s.update(day=today, day_start=equity, daily_tripped=False)
        if not s.get("peak_tripped") and equity > float(s["peak"]):
            s["peak"], s["peak_time"] = equity, now.isoformat(timespec="minutes")
        peak, day_start = float(s["peak"]), float(s.get("day_start") or equity)
        dd_day = 100.0 * (equity / day_start - 1)
        dd_peak = 100.0 * (equity / peak - 1)
        res.update(dd_day=dd_day, dd_peak=dd_peak, peak=peak)
        need = max(1, int(c["confirm_runs"]))

        if dd_day <= -float(c["daily_limit_pct"]) and not s.get("daily_tripped"):
            s["daily_tripped"] = True
            res["messages"].append(f"ДНЕВНОЙ СТОП: капитал {dd_day:+.1f}% с начала суток UTC (лимит -{c['daily_limit_pct']:g}%). "
                                   "Новые позиции не открываю до 00:00 UTC (03:00 по Минску)")
        if dd_peak <= -float(c["peak_limit_pct"]):
            if not s.get("peak_tripped"):
                s["peak_bad_runs"] = int(s.get("peak_bad_runs", 0)) + 1
                if s["peak_bad_runs"] >= need:
                    s["peak_tripped"], s["peak_tripped_at"] = True, now.isoformat(timespec="minutes")
                    res["messages"].append(f"СТОП ОТ ПИКА ВКЛЮЧЁН: капитал {dd_peak:+.1f}% от максимума {peak:.2f} "
                                           f"(лимит -{c['peak_limit_pct']:g}%)")
                else:
                    res["block_new"] = True
                    res["messages"].append(f"просадка от пика {dd_peak:+.1f}% — новые входы пропускаю, проверю ещё раз")
        else:
            s["peak_bad_runs"] = 0

        if c["close_all_enabled"] and dd_peak <= -float(c["close_all_pct"]) and not s.get("closed_all_at"):
            s["close_bad_runs"] = int(s.get("close_bad_runs", 0)) + 1
            if s["close_bad_runs"] >= need:
                res["close_all"] = True
                res["messages"].append(f"ЗАКРЫВАЮ ВСЕ ПОЗИЦИИ: просадка {dd_peak:+.1f}% от пика (порог -{c['close_all_pct']:g}%)")
        elif not (dd_peak <= -float(c["close_all_pct"])):
            s["close_bad_runs"] = 0

        if s.get("daily_tripped") or res["close_all"]:
            res["block_new"] = True
        if s.get("peak_tripped"):
            res["block_new"] = True
            res["messages"].append(f"стоп от пика действует с {s.get('peak_tripped_at')}: новые позиции не открываю. "
                                   f"Чтобы снять — положите в папку бота пустой файл «{RESET_FILE_NAME}»")
        self._save(s)
        return res

    def mark_closed_all(self, now=None):
        s, _ = self._load()
        if s:
            s["closed_all_at"] = (now or datetime.now(timezone.utc)).isoformat(timespec="minutes")
            s["peak_tripped"] = True
            s["peak_tripped_at"] = s.get("peak_tripped_at") or s["closed_all_at"]
            self._save(s)

    def status_line(self, res):
        if not self.c["enabled"]:
            return "аварийный стоп выключен"
        st = "НОВЫЕ ВХОДЫ ЗАПРЕЩЕНЫ" if res["block_new"] else "входы разрешены"
        pk = f"{res['peak']:.2f}" if res.get("peak") else "?"
        return (f"аварийный стоп: {st}. За сутки UTC {res['dd_day']:+.1f}% (лимит -{self.c['daily_limit_pct']:g}%), "
                f"от пика {pk}: {res['dd_peak']:+.1f}% (лимит -{self.c['peak_limit_pct']:g}%)")
