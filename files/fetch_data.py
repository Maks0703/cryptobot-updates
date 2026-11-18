"""Загрузка исторических свечей BTC/USDT (спот).

Сначала пробуем публичный API Bybit (/v5/market/kline, category=spot).
Если Bybit недоступен (например, гео-блокировка), берём публичный архив
Binance (data-api.binance.vision) — это тоже спот BTC/USDT, цены отличаются
от Bybit на доли процента.
"""
import argparse, json, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
import pandas as pd

BYBIT = "https://api.bybit.com/v5/market/kline"
BINANCE = "https://data-api.binance.vision/api/v3/klines"
BYBIT_IV = {"1d": "D", "4h": "240"}
MS = {"1d": 86_400_000, "4h": 14_400_000}


def _get(url, params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "cryptobot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def fetch_bybit(tf, start_ms, end_ms):
    rows, cur_end = [], end_ms
    while cur_end > start_ms:
        d = _get(BYBIT, {"category": "spot", "symbol": "BTCUSDT", "interval": BYBIT_IV[tf],
                         "start": start_ms, "end": cur_end, "limit": 1000})
        if d.get("retCode") != 0:
            raise RuntimeError(d)
        lst = d["result"]["list"]  # newest first
        if not lst:
            break
        rows += [[int(x[0]), *map(float, x[1:6])] for x in lst]
        cur_end = int(lst[-1][0]) - 1
        time.sleep(0.2)
    return rows


def fetch_binance(tf, start_ms, end_ms):
    rows, cur = [], start_ms
    while cur < end_ms:
        d = _get(BINANCE, {"symbol": "BTCUSDT", "interval": tf, "startTime": cur,
                           "endTime": end_ms, "limit": 1000})
        if not d:
            break
        rows += [[int(x[0]), *map(float, x[1:6])] for x in d]
        cur = int(d[-1][0]) + MS[tf]
        time.sleep(0.2)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--timeframes", default="1d,4h")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=a.days)).timestamp() * 1000)
    meta = {}
    for tf in a.timeframes.split(","):
        source = "bybit"
        try:
            rows = fetch_bybit(tf, start_ms, end_ms)
        except Exception as e:
            print(f"[{tf}] Bybit недоступен ({str(e)[:120]}), берём Binance public data", file=sys.stderr)
            source = "binance-public-data (data-api.binance.vision)"
            rows = fetch_binance(tf, start_ms, end_ms)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates("ts").sort_values("ts")
        # отбрасываем незакрытую текущую свечу
        df = df[df.ts + MS[tf] <= end_ms]
        df["time"] = pd.to_datetime(df.ts, unit="ms", utc=True)
        path = f"data/BTCUSDT_{tf}.csv"
        df.to_csv(path, index=False)
        meta[tf] = {"source": source, "rows": len(df), "first": str(df.time.iloc[0]),
                    "last": str(df.time.iloc[-1])}
        print(tf, meta[tf])
    json.dump(meta, open("data/meta.json", "w"), indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
