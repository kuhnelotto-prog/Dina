#!/usr/bin/env python3
"""Compare OLD baseline (before fixes) vs NEW baseline (look-ahead fix + commission)."""
import sys, os, time, logging, json
import pandas as pd
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from backtester import Backtester

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT"]
START = datetime(2026, 1, 12)
END = datetime(2026, 4, 12)

def fetch(sym, start_dt, end_dt, gran="4H"):
    all_c = []
    et = int(end_dt.timestamp() * 1000)
    st = int(start_dt.timestamp() * 1000)
    ce = et
    for _ in range(10):
        p = {"symbol": sym, "granularity": gran, "limit": 1000,
             "endTime": ce, "startTime": st, "productType": "USDT-FUTURES"}
        r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=p, timeout=30).json()
        if r.get("code") != "00000" or not r.get("data"):
            break
        for c in r["data"]:
            all_c.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        if len(r["data"]) < 1000:
            break
        e = int(r["data"][-1][0])
        if e >= ce:
            break
        ce = e - 1
        time.sleep(0.1)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

def fetch1d(sym, limit=200):
    p = {"symbol": sym, "granularity": "1D", "limit": limit, "productType": "USDT-FUTURES"}
    r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=p, timeout=30).json()
    if r.get("code") != "00000" or not r.get("data"):
        return pd.DataFrame()
    rows = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in r["data"]]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

print("Loading data...")
btc_df = fetch("BTCUSDT", START, END)
btc_1d = fetch1d("BTCUSDT")
data = {}
for s in SYMBOLS:
    data[s] = btc_df if s == "BTCUSDT" else fetch(s, START, END)
    time.sleep(0.3)
    print(f"  {s}: {len(data[s])} candles")

# Old results from filter_analysis.json (before fixes)
old = {
    "BTCUSDT":  {"trades": 7, "wr": 28.6, "pnl": -0.408},
    "ETHUSDT":  {"trades": 2, "wr": 50.0, "pnl": -0.131},
    "XRPUSDT":  {"trades": 6, "wr": 50.0, "pnl": 0.958},
    "DOGEUSDT": {"trades": 7, "wr": 28.6, "pnl": 0.157},
    "LINKUSDT": {"trades": 4, "wr": 50.0, "pnl": 0.002},
}

print()
print("=" * 90)
print("BASELINE COMPARISON: OLD (no fix) vs NEW (open[i+1] + commission)")
print("=" * 90)
header = f"{'Symbol':<10} {'OLD tr':>7} {'NEW tr':>7} {'OLD WR%':>8} {'NEW WR%':>8} {'OLD PnL%':>9} {'NEW PnL%':>9}"
print(header)
print("-" * 90)

total_old_t = 0
total_new_t = 0
total_old_p = 0.0
total_new_p = 0.0
new_results = {}

for s in SYMBOLS:
    df = data[s]
    if df.empty or len(df) < 100:
        continue
    bt = Backtester(initial_balance=10000.0)
    res = bt.run(df=df, symbol=s, btc_df=btc_df, btc_1d_df=btc_1d)
    t = res.total_trades
    wr = (res.winning_trades / t * 100) if t > 0 else 0
    pnl = (res.final_balance - 10000) / 100
    o = old[s]
    new_results[s] = {"trades": t, "wr": round(wr, 1), "pnl": round(pnl, 3)}
    print(f"{s:<10} {o['trades']:>7} {t:>7} {o['wr']:>8.1f} {wr:>8.1f} {o['pnl']:>9.3f} {pnl:>9.3f}")
    total_old_t += o["trades"]
    total_new_t += t
    total_old_p += o["pnl"]
    total_new_p += pnl

print("-" * 90)
print(f"{'TOTAL':<10} {total_old_t:>7} {total_new_t:>7} {'':>8} {'':>8} {total_old_p:>9.3f} {total_new_p:>9.3f}")
print()
print(f"Delta trades: {total_new_t - total_old_t:+d}")
print(f"Delta PnL:    {total_new_p - total_old_p:+.3f}%")

# Save comparison
comparison = {
    "old_baseline": {"total_trades": total_old_t, "total_pnl_pct": round(total_old_p, 3), "by_symbol": old},
    "new_baseline": {"total_trades": total_new_t, "total_pnl_pct": round(total_new_p, 3), "by_symbol": new_results},
    "fixes_applied": ["look-ahead bias: entry=open[i+1]", "commission: 0.12% round trip"],
    "delta_trades": total_new_t - total_old_t,
    "delta_pnl_pct": round(total_new_p - total_old_p, 3),
}
with open("baseline_comparison.json", "w") as f:
    json.dump(comparison, f, indent=2)
print("\nSaved to baseline_comparison.json")
