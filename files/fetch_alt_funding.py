"""Per-coin USDT-M funding history from data.binance.vision monthly archives."""
import io, sys, zipfile, urllib.request
from datetime import date
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
COINS = ["ETH","SOL","XRP","BNB","DOGE","ADA","AVAX","LINK"]
def get(u):
    r = urllib.request.Request(u, headers={"User-Agent":"cryptobot/1.0"})
    with urllib.request.urlopen(r, timeout=30) as x: return x.read()
def one(args):
    s, y, m = args
    u = f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{y}-{m:02d}.zip"
    try:
        z = zipfile.ZipFile(io.BytesIO(get(u))); f = pd.read_csv(z.open(z.namelist()[0])); f.columns=[c.strip() for c in f.columns]; return f
    except Exception as e:
        return None
months=[]; y,m=2021,1; t=date.today()
while (y,m)<(t.year,t.month):
    months.append((y,m)); m+=1
    if m==13: y,m=y+1,1
for c in COINS:
    s=c+"USDT"
    with ThreadPoolExecutor(8) as ex: fr=list(ex.map(one,[(s,y,m) for y,m in months]))
    ok=[f for f in fr if f is not None]
    miss=[f"{y}-{m:02d}" for (y,m),f in zip(months,fr) if f is None]
    if ok:
        pd.concat(ok).to_csv(f"data/{s}_funding.csv",index=False)
    print(s, "months ok", len(ok), "missing", miss)
