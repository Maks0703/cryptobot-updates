#!/usr/bin/env python3
"""Paper DCA on Kraken: buy a fixed USD amount of BTC in the dca-btc paper workspace.
Hard limits: budget 1000 USD total, 10 USD per buy, spot only, paper only."""
import json, subprocess, sys, datetime
WS, PAIR, AMOUNT, BUDGET = "dca-btc", "BTCUSD", 10.0, 1000.0
def k(*a):
    r = subprocess.run(["kraken", *a, "-o", "json"], capture_output=True, text=True,
                       env={"KRAKEN_WORKSPACE": WS, "PATH": "/usr/local/bin:/usr/bin:/bin"})
    if r.returncode: sys.exit(f"kraken {' '.join(a)} failed: {r.stderr.strip()[-500:]}")
    return json.loads(r.stdout)
t = k("ticker", PAIR); price = float(next(iter(t.values()))["ask_price"])
bal = k("paper", "balance")
print(json.dumps({"balance_before": bal}, default=str))
hist = k("paper", "history")
spent = sum(float(x.get("cost", 0)) for x in (hist if isinstance(hist, list) else hist.get("trades", [])) if x.get("side") == "buy") if hist else 0
if spent + AMOUNT > BUDGET: sys.exit(f"Budget reached: spent {spent:.2f} of {BUDGET}")
vol = f"{AMOUNT/price:.8f}"
res = k("paper", "buy", PAIR, vol, "--reason", f"DCA {AMOUNT} USD {datetime.date.today()}")
print(json.dumps({"price": price, "volume": vol, "result": res}, default=str))
