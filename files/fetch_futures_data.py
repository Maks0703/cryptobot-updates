"""Fetch long BTCUSDT 4h spot klines (data-api.binance.vision) + USDT-M funding history (data.binance.vision)."""
import io, json, time, urllib.request, urllib.parse, zipfile
from datetime import datetime, timezone, date
import pandas as pd

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cryptobot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def klines(start="2020-01-01"):
    cur = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp()*1000)
    end = int(time.time()*1000); rows = []
    while cur < end:
        q = urllib.parse.urlencode({"symbol":"BTCUSDT","interval":"4h","startTime":cur,"limit":1000})
        d = json.loads(get("https://data-api.binance.vision/api/v3/klines?"+q))
        if not d: break
        rows += [[int(x[0]), *map(float, x[1:6]), int(x[6])] for x in d]
        cur = int(d[-1][0]) + 14_400_000; time.sleep(0.15)
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume","close_ts"])
    df = df[df.close_ts < end]  # only closed candles
    df["time"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    df.drop(columns="close_ts").to_csv("data/BTCUSDT_4h_long.csv", index=False)
    print("klines", len(df), df.time.iloc[0], df.time.iloc[-1])

def funding():
    frames = []; y, m = 2020, 1; today = date.today()
    while (y, m) < (today.year, today.month):
        url = f"https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{y}-{m:02d}.zip"
        try:
            z = zipfile.ZipFile(io.BytesIO(get(url)))
            f = pd.read_csv(z.open(z.namelist()[0]))
            frames.append(f)
        except Exception as e:
            print("miss", y, m, e)
        m += 1
        if m == 13: y, m = y+1, 1
    f = pd.concat(frames)
    f.columns = [c.strip() for c in f.columns]
    print(f.columns.tolist())
    f.to_csv("data/BTCUSDT_funding.csv", index=False)
    print("funding rows", len(f))

if __name__ == "__main__":
    klines(); funding()
