"""Загрузка 4h свечей спот альткоинов (USDT) с data-api.binance.vision, 2021-01-01 .. сейчас."""
import json, time, urllib.request, urllib.parse, sys
from datetime import datetime, timezone
import pandas as pd
URL = "https://data-api.binance.vision/api/v3/klines"
SYMS = ["BTC","ETH","SOL","XRP","BNB","DOGE","ADA","AVAX","LINK"]
def get(p):
    for i in range(5):
        try:
            r = urllib.request.urlopen(urllib.request.Request(URL+"?"+urllib.parse.urlencode(p), headers={"User-Agent":"cryptobot"}), timeout=30)
            return json.loads(r.read())
        except Exception as e:
            time.sleep(2); err = e
    raise err
start = int(datetime(2021,1,1,tzinfo=timezone.utc).timestamp()*1000)
end = int(datetime.now(timezone.utc).timestamp()*1000)
for s in SYMS:
    sym = s+"USDT"; rows=[]; cur=start
    while cur < end:
        d = get({"symbol":sym,"interval":"4h","startTime":cur,"endTime":end,"limit":1000})
        if not d: break
        rows += [[int(x[0]),*map(float,x[1:6])] for x in d]
        cur = int(d[-1][0]) + 14_400_000
        time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df = df[df.ts + 14_400_000 <= end]  # only closed candles
    df["time"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    df.to_csv(f"data/{sym}_4h_alts.csv", index=False)
    print(sym, len(df), df.time.iloc[0], df.time.iloc[-1], flush=True)
