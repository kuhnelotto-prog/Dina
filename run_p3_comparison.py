#!/usr/bin/env python3
"""P3 filter tuning comparison: baseline_p1_fixed vs P3 tuned."""
import sys, os, time, logging, json, math
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

def compute_sharpe(trades, initial_balance=10000.0):
    """Annualized Sharpe from trade returns."""
    if not trades:
        return 0.0
    returns = [t.pnl_usd / initial_balance for t in trades]
    if len(returns) < 2:
        return 0.0
    mean_r = sum(returns) / len(returns)
    var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r = math.sqrt(var_r) if var_r > 0 else 0.001
    # Annualize: assume ~6 trades/month * 12 = 72 trades/year
    trades_per_year = 72
    return (mean_r / std_r) * math.sqrt(trades_per_year)

print("Loading data...")
btc_df = fetch("BTCUSDT", START, END)
btc_1d = fetch1d("BTCUSDT")
data = {}
for s in SYMBOLS:
    data[s] = btc_df if s == "BTCUSDT" else fetch(s, START, END)
    time.sleep(0.3)
    print(f"  {s}: {len(data[s])} candles")

# P1 fixed baseline (from baseline_comparison.json)
p1 = {
    "BTCUSDT":  {"trades": 7, "wr": 28.6, "pnl": -0.492},
    "ETHUSDT":  {"trades": 2, "wr": 50.0, "pnl": -0.155},
    "XRPUSDT":  {"trades": 6, "wr": 50.0, "pnl": 0.885},
    "DOGEUSDT": {"trades": 7, "wr": 28.6, "pnl": 0.073},
    "LINKUSDT": {"trades": 4, "wr": 50.0, "pnl": -0.046},
}

print()
print("=" * 100)
print("P3 FILTER TUNING: P1 fixed baseline vs P3 tuned")
print("Changes: composite thresholds lowered + adx_growth removed")
print("=" * 100)
header = f"{'Symbol':<10} {'P1 tr':>6} {'P3 tr':>6} {'P1 WR%':>7} {'P3 WR%':>7} {'P1 PnL%':>8} {'P3 PnL%':>8} {'P3 MaxDD%':>9}"
print(header)
print("-" * 100)

total_p1_t = 0; total_p3_t = 0
total_p1_p = 0.0; total_p3_p = 0.0
all_trades = []
p3_results = {}

for s in SYMBOLS:
    df = data[s]
    if df.empty or len(df) < 100:
        continue
    bt = Backtester(initial_balance=10000.0)
    res = bt.run(df=df, symbol=s, btc_df=btc_df, btc_1d_df=btc_1d)
    t = res.total_trades
    wr = (res.winning_trades / t * 100) if t > 0 else 0
    pnl = (res.final_balance - 10000) / 100
    dd = res.max_drawdown_pct
    o = p1[s]
    p3_results[s] = {"trades": t, "wr": round(wr, 1), "pnl": round(pnl, 3), "max_dd": round(dd, 2)}
    all_trades.extend(res.trades)
    print(f"{s:<10} {o['trades']:>6} {t:>6} {o['wr']:>7.1f} {wr:>7.1f} {o['pnl']:>8.3f} {pnl:>8.3f} {dd:>9.2f}")
    total_p1_t += o["trades"]; total_p3_t += t
    total_p1_p += o["pnl"]; total_p3_p += pnl

print("-" * 100)
print(f"{'TOTAL':<10} {total_p1_t:>6} {total_p3_t:>6} {'':>7} {'':>7} {total_p1_p:>8.3f} {total_p3_p:>8.3f}")

sharpe = compute_sharpe(all_trades)
print(f"\nDelta trades: {total_p3_t - total_p1_t:+d}")
print(f"Delta PnL:    {total_p3_p - total_p1_p:+.3f}%")
print(f"P3 Sharpe:    {sharpe:.2f}")
print(f"P3 total PnL: {total_p3_p:.3f}%")

# Decision
if total_p3_p > 1.0:
    print(f"\n>>> PASS: PnL {total_p3_p:.3f}% > 1.0% threshold. Ready to commit.")
    decision = "COMMIT"
else:
    print(f"\n>>> FAIL: PnL {total_p3_p:.3f}% <= 1.0% threshold. DO NOT commit.")
    decision = "NO_COMMIT"

# Save
result = {
    "period": f"{START.date()} -> {END.date()}",
    "p1_fixed_baseline": {"total_trades": total_p1_t, "total_pnl_pct": round(total_p1_p, 3), "by_symbol": p1},
    "p3_tuned": {"total_trades": total_p3_t, "total_pnl_pct": round(total_p3_p, 3),
                 "sharpe": round(sharpe, 2), "by_symbol": p3_results},
    "changes": [
        "composite_threshold LONG: 0.30->0.20 (BULL), 0.45->0.30 (BEAR)",
        "composite_threshold SHORT: 0.45->0.35 (BULL), 0.30->0.45 (BEAR)",
        "adx_growth: removed (was overtight)",
    ],
    "delta_trades": total_p3_t - total_p1_t,
    "delta_pnl_pct": round(total_p3_p - total_p1_p, 3),
    "decision": decision,
}
with open("baseline_p3_filters_tuned.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to baseline_p3_filters_tuned.json")
