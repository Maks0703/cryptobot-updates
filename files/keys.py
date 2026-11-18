"""Хранит демо-ключ Bybit в файле keys.json рядом с ботом (только на вашем ПК).
Запуск `py -3.12 keys.py` — ввести/проверить ключ. Удалите keys.json, чтобы ввести новый."""
import json, os, sys
from pathlib import Path

FILE = Path(__file__).with_name("keys.json")


def _clean(s):
    return "".join(s.split())  # убираем все пробелы и переносы


def load():
    """Ключ из переменных окружения, иначе из keys.json."""
    k, s = _clean(os.environ.get("BYBIT_API_KEY", "")), _clean(os.environ.get("BYBIT_API_SECRET", ""))
    if k and s:
        return k, s
    if FILE.exists():
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return _clean(d.get("key", "")), _clean(d.get("secret", ""))
    return "", ""


def check(k, s):
    from pybit.unified_trading import HTTP
    c = HTTP(testnet=False, demo=True, api_key=k, api_secret=s, recv_window=10000)
    r = c.get_wallet_balance(accountType="UNIFIED")
    return float(r["result"]["list"][0]["totalEquity"])


def setup():
    for attempt in range(3):
        k, s = load()
        if not (k and s):
            print("\nНужен ДЕМО API-ключ Bybit (права: Спот + Контракты, вывод НЕ включать).")
            k = _clean(input("Вставьте ДЕМО API Key и нажмите Enter: "))
            s = _clean(input("Вставьте ДЕМО API Secret и нажмите Enter: "))
        try:
            eq = check(k, s)
        except Exception as e:
            print(f"\nКлюч не подошёл: {str(e)[:160]}")
            print("Скорее всего, секрет вставлен не полностью или ключ не из Демо-торговли. Попробуем ещё раз.")
            if FILE.exists():
                FILE.unlink()
            os.environ.pop("BYBIT_API_KEY", None); os.environ.pop("BYBIT_API_SECRET", None)
            continue
        FILE.write_text(json.dumps({"key": k, "secret": s}), encoding="utf-8")
        print(f"\nКлюч работает, баланс демо-счёта: {eq:,.2f} USD. Ключ сохранён, больше вводить не нужно.")
        return True
    print("\nТри попытки не удались. Сделайте скриншот окна (замажьте ключ) и пришлите Криптоботу.")
    return False


if __name__ == "__main__":
    sys.exit(0 if setup() else 1)
