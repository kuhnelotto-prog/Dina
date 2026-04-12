#!/usr/bin/env python3
"""P3 v2: BTC, ETH, XRP, LINK, SOL (DOGE replaced by SOL)."""
import sys, os, time, logging, json, math
import pandas as pd
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from backtester import Backtester

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "LINKUSDT", "SOLUSDT"]
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
    if not trades:
        return 0.0
    returns = [t.pnl_usd / initial_balance for t in trades]
    if len(returns) < 2:
        return 0.0
    mean_r = sum(returns) / len(returns)
    var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r = math.sqrt(var_r) if var_r > 0 else 0.001
    trades_per_year = 72
    return (mean_r / std_r) * math.sqrt(trades_per_year)

def compute_max_dd(trades, initial_balance=10000.0):
    """Compute portfolio-level max drawdown across all symbols."""
    balance = initial_balance
    peak = initial_balance
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_time):
        balance += t.pnl_usd
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd

print("Loading data...")
btc_df = fetch("BTCUSDT", START, END)
btc_1d = fetch1d("BTCUSDT")
data = {}
for s in SYMBOLS:
    if s == "BTCUSDT":
        data[s] = btc_df
    else:
        data[s] = fetch(s, START, END)
        time.sleep(0.3)
    print(f"  {s}: {len(data[s])} candles")

print()
print("=" * 100)
print("P3 v2: BTC, ETH, XRP, LINK, SOL (DOGE->SOL)")
print("Thresholds: LONG 0.20/0.30, SHORT 0.35/0.45, adx_growth removed")
print("=" * 100)
header = f"{'Symbol':<10} {'Trades':>6} {'WR%':>6} {'PnL%':>8} {'MaxDD%':>8} {'Sharpe':>7}"
print(header)
print("-" * 100)

total_trades = 0
total_pnl = 0.0
all_trades = []
results = {}

for s in SYMBOLS:
    df = data[s]
    if df.empty or len(df) < 100:
        print(f"  {s}: SKIP (insufficient data)")
        continue
    bt = Backtester(initial_balance=10000.0)
    res = bt.run(df=df, symbol=s, btc_df=btc_df, btc_1d_df=btc_1d)
    t = res.total_trades
    wr = (res.winning_trades / t * 100) if t > 0 else 0
    pnl = (res.final_balance - 10000) / 100
    dd = res.max_drawdown_pct
    sym_sharpe = compute_sharpe(res.trades)
    results[s] = {"trades": t, "wr": round(wr, 1), "pnl": round(pnl, 3),
                  "max_dd": round(dd, 2), "sharpe": round(sym_sharpe, 2)}
    all_trades.extend(res.trades)
    print(f"{s:<10} {t:>6} {wr:>6.1f} {pnl:>8.3f} {dd:>8.2f} {sym_sharpe:>7.2f}")
    total_trades += t
    total_pnl += pnl

print("-" * 100)
portfolio_sharpe = compute_sharpe(all_trades)
portfolio_dd = compute_max_dd(all_trades)
print(f"{'TOTAL':<10} {total_trades:>6} {'':>6} {total_pnl:>8.3f} {portfolio_dd:>8.2f} {portfolio_sharpe:>7.2f}")

print(f"\nTotal trades: {total_trades}")
print(f"Total PnL:    {total_pnl:.3f}%")
print(f"Sharpe:       {portfolio_sharpe:.2f}")
print(f"Max DD:       {portfolio_dd:.2f}%")

# Decision
passes_pnl = total_pnl > 1.0
passes_trades = total_trades >= 25
if passes_pnl and passes_trades:
    print(f"\n>>> PASS: PnL {total_pnl:.3f}% > 1.0% AND trades {total_trades} >= 25. Ready to commit.")
    decision = "COMMIT"
elif not passes_pnl:
    print(f"\n>>> FAIL: PnL {total_pnl:.3f}% <= 1.0%")
    decision = "NO_COMMIT"
else:
    print(f"\n>>> FAIL: trades {total_trades} < 25")
    decision = "NO_COMMIT"

# Why 26→21 trades explanation
explanation = (
    "P1 had 26 trades, P3 v1 had 21 trades despite LOWER thresholds. "
    "Reason: implicit serialization in backtester — only 1 position per symbol at a time. "
    "With P3 lower thresholds, winning trades survive longer (trailing steps 2-3), "
    "blocking the slot for new entries. Fewer but higher-quality trades."
)

result = {
    "period": f"{START.date()} -> {END.date()}",
    "symbols": SYMBOLS,
    "changes": [
        "DOGE removed, SOL added",
        "composite_threshold LONG: 0.20 (BULL), 0.30 (BEAR)",
        "composite_threshold SHORT: 0.35 (BULL), 0.45 (BEAR)",
        "adx_growth: removed",
    ],
    "results": {
        "total_trades": total_trades,
        "total_pnl_pct": round(total_pnl, 3),
        "sharpe": round(portfolio_sharpe, 2),
        "max_dd_pct": round(portfolio_dd, 2),
        "by_symbol": results,
    },
    "why_fewer_trades": explanation,
    "decision": decision,
}
with open("baseline_p3_v2.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to baseline_p3_v2.json")
