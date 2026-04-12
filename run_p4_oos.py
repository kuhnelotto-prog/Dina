#!/usr/bin/env python3
"""
P4 OOS validation after Step 4 fix (trailing SL instead of hard TP).
Commit: ed1249e

Train period: 2026-01-12 -> 2026-04-12
OOS period:   2025-10-12 -> 2026-01-12

Compares 3 columns:
  1. Train (Hard TP) - P3 with hard TP at 2.5R (commit 21f14b1)
  2. Train (Trailing) - P4 with trailing SL at +1.2R (commit ed1249e)
  3. OOS (Trailing) - P4 on out-of-sample period
"""
import sys, os, time, logging, json, math
import pandas as pd
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from backtester import Backtester

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "LINKUSDT", "SOLUSDT"]

OOS_START = datetime(2025, 10, 12)
OOS_END = datetime(2026, 1, 12)

# Historical results for comparison
TRAIN_HARD_TP = {
    "total_trades": 52, "total_pnl_pct": 1.308, "sharpe": 0.66,
    "max_dd_pct": 1.97, "win_rate_pct": 34.6,
}
TRAIN_TRAILING = {
    "total_trades": 33, "total_pnl_pct": 2.276, "sharpe": 1.24,
    "max_dd_pct": 1.62,
    "by_symbol": {
        "BTCUSDT": {"trades": 1, "wr": 100.0, "pnl": 0.994},
        "ETHUSDT": {"trades": 8, "wr": 25.0, "pnl": -0.086},
        "XRPUSDT": {"trades": 10, "wr": 40.0, "pnl": 0.067},
        "LINKUSDT": {"trades": 12, "wr": 41.7, "pnl": -0.321},
        "SOLUSDT": {"trades": 2, "wr": 50.0, "pnl": 1.622},
    }
}


def fetch(sym, start_dt, end_dt, gran="4H"):
    all_c = []
    et = int(end_dt.timestamp() * 1000)
    st = int(start_dt.timestamp() * 1000)
    ce = et
    for _ in range(15):
        p = {"symbol": sym, "granularity": gran, "limit": 1000,
             "endTime": ce, "productType": "USDT-FUTURES"}
        r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=p, timeout=30).json()
        if r.get("code") != "00000" or not r.get("data"):
            break
        for c in r["data"]:
            ts = int(c[0])
            if ts >= st:
                all_c.append([ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        earliest_ts = int(r["data"][-1][0])
        if earliest_ts <= st:
            break
        if len(r["data"]) < 1000:
            break
        if earliest_ts >= ce:
            break
        ce = earliest_ts - 1
        time.sleep(0.15)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df


def fetch1d(sym, limit=300):
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
    return (mean_r / std_r) * math.sqrt(72)


def compute_max_dd(trades, initial_balance=10000.0):
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


def compute_win_rate(trades):
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.pnl_usd > 0) / len(trades) * 100


# ── Load OOS data ──
print("=" * 110)
print("P4 OOS VALIDATION (Step 4 fix: trailing SL instead of hard TP)")
print(f"OOS period: {OOS_START.date()} -> {OOS_END.date()}")
print("=" * 110)
print()
print("Loading OOS data...")

btc_df = fetch("BTCUSDT", OOS_START, OOS_END)
btc_1d = fetch1d("BTCUSDT", limit=300)
data = {}
for s in SYMBOLS:
    if s == "BTCUSDT":
        data[s] = btc_df
    else:
        data[s] = fetch(s, OOS_START, OOS_END)
        time.sleep(0.3)
    print(f"  {s}: {len(data[s])} candles")

# ── Run OOS backtest ──
print()
print("OOS per-symbol results:")
print(f"{'Symbol':<10} {'Trades':>6} {'WR%':>6} {'PnL%':>8} {'MaxDD%':>8} {'Sharpe':>7}")
print("-" * 60)

total_trades = 0
total_pnl = 0.0
all_trades = []
oos_results = {}

for s in SYMBOLS:
    df = data[s]
    if df.empty or len(df) < 100:
        print(f"  {s}: SKIP ({len(df)} candles)")
        continue
    bt = Backtester(initial_balance=10000.0)
    res = bt.run(df=df, symbol=s, btc_df=btc_df, btc_1d_df=btc_1d)
    t = res.total_trades
    wr = (res.winning_trades / t * 100) if t > 0 else 0
    pnl = (res.final_balance - 10000) / 100
    dd = res.max_drawdown_pct
    sh = compute_sharpe(res.trades)
    oos_results[s] = {"trades": t, "wr": round(wr, 1), "pnl": round(pnl, 3),
                      "max_dd": round(dd, 2), "sharpe": round(sh, 2)}
    all_trades.extend(res.trades)
    print(f"{s:<10} {t:>6} {wr:>6.1f} {pnl:>8.3f} {dd:>8.2f} {sh:>7.2f}")
    total_trades += t
    total_pnl += pnl

print("-" * 60)
p_sharpe = compute_sharpe(all_trades)
p_dd = compute_max_dd(all_trades)
p_wr = compute_win_rate(all_trades)
print(f"{'TOTAL':<10} {total_trades:>6} {p_wr:>6.1f} {total_pnl:>8.3f} {p_dd:>8.2f} {p_sharpe:>7.2f}")

# Compute train trailing WR
train_wins = sum(round(v["trades"] * v["wr"] / 100) for v in TRAIN_TRAILING["by_symbol"].values())
train_total = sum(v["trades"] for v in TRAIN_TRAILING["by_symbol"].values())
train_wr = train_wins / train_total * 100 if train_total > 0 else 0

# ── 3-column comparison ──
print()
print("=" * 110)
print("3-COLUMN COMPARISON")
print("=" * 110)
header = f"{'Metric':<15} {'Train(HardTP)':>14} {'Train(Trail)':>14} {'OOS(Trail)':>14} {'OOS Status':>12}"
print(header)
print("-" * 110)

rows = [
    ("Trades",   TRAIN_HARD_TP["total_trades"], TRAIN_TRAILING["total_trades"], total_trades, ""),
    ("PnL %",    TRAIN_HARD_TP["total_pnl_pct"], TRAIN_TRAILING["total_pnl_pct"], total_pnl,
     "PASS" if total_pnl > 0 else "FAIL"),
    ("Sharpe",   TRAIN_HARD_TP["sharpe"], TRAIN_TRAILING["sharpe"], p_sharpe,
     "PASS" if p_sharpe > 0.3 else "WARN" if p_sharpe > 0 else "FAIL"),
    ("MaxDD %",  TRAIN_HARD_TP["max_dd_pct"], TRAIN_TRAILING["max_dd_pct"], p_dd,
     "OK" if p_dd <= TRAIN_TRAILING["max_dd_pct"] * 2 else "WARN"),
    ("WinRate %", TRAIN_HARD_TP.get("win_rate_pct", 34.6), train_wr, p_wr,
     "OK" if p_wr >= 30 else "WARN"),
]

for name, v1, v2, v3, status in rows:
    print(f"{name:<15} {v1:>14.3f} {v2:>14.3f} {v3:>14.3f} {status:>12}")

# ── Per-symbol OOS vs Train(Trailing) ──
print()
print("Per-symbol: Train(Trailing) vs OOS(Trailing)")
print(f"{'Symbol':<10} {'Tr PnL%':>8} {'OOS PnL%':>9} {'Delta':>8} {'Tr WR%':>7} {'OOS WR%':>8}")
print("-" * 60)
for s in SYMBOLS:
    tr = TRAIN_TRAILING["by_symbol"].get(s, {"pnl": 0, "wr": 0})
    oo = oos_results.get(s, {"pnl": 0, "wr": 0})
    d = oo["pnl"] - tr["pnl"]
    print(f"{s:<10} {tr['pnl']:>8.3f} {oo['pnl']:>9.3f} {d:>+8.3f} {tr['wr']:>7.1f} {oo['wr']:>8.1f}")

# ── Verdict ──
print()
print("=" * 110)
if total_pnl > 0 and p_sharpe > 0.3:
    verdict = "PASS"
    print(f">>> OOS VERDICT: PASS | PnL {total_pnl:.3f}% > 0, Sharpe {p_sharpe:.2f} > 0.3")
    print("    Step 4 fix generalizes. Safe to keep.")
elif total_pnl > 0:
    verdict = "MARGINAL"
    print(f">>> OOS VERDICT: MARGINAL | PnL {total_pnl:.3f}% > 0, Sharpe {p_sharpe:.2f} <= 0.3")
else:
    verdict = "FAIL"
    print(f">>> OOS VERDICT: FAIL | PnL {total_pnl:.3f}% <= 0")
print("=" * 110)

# ── Save ──
result = {
    "oos_period": f"{OOS_START.date()} -> {OOS_END.date()}",
    "train_period": "2026-01-12 -> 2026-04-12",
    "commit": "ed1249e",
    "change": "Step 4: hard TP at 2.5R -> trailing SL at +1.2R",
    "symbols": SYMBOLS,
    "train_hard_tp": TRAIN_HARD_TP,
    "train_trailing": {
        "total_trades": TRAIN_TRAILING["total_trades"],
        "total_pnl_pct": TRAIN_TRAILING["total_pnl_pct"],
        "sharpe": TRAIN_TRAILING["sharpe"],
        "max_dd_pct": TRAIN_TRAILING["max_dd_pct"],
        "win_rate_pct": round(train_wr, 1),
    },
    "oos_trailing": {
        "total_trades": total_trades,
        "total_pnl_pct": round(total_pnl, 3),
        "sharpe": round(p_sharpe, 2),
        "max_dd_pct": round(p_dd, 2),
        "win_rate_pct": round(p_wr, 1),
        "by_symbol": oos_results,
    },
    "verdict": verdict,
}
with open("baseline_p4_oos.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to baseline_p4_oos.json")
