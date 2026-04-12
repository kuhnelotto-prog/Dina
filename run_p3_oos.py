#!/usr/bin/env python3
"""
P3 Out-of-Sample validation.
Train period: 2026-01-12 -> 2026-04-12 (where P3 was optimized)
OOS period:   2025-10-12 -> 2026-01-12 (90 days BEFORE train window)

NO parameter changes. Same P3 config as commit 21f14b1.
"""
import sys, os, time, logging, json, math
import pandas as pd
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from backtester import Backtester

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "LINKUSDT", "SOLUSDT"]

# OOS period: 90 days BEFORE train window
OOS_START = datetime(2025, 10, 12)
OOS_END = datetime(2026, 1, 12)

# Train results (from baseline_p3_v2.json, commit 21f14b1)
TRAIN_RESULTS = {
    "total_trades": 52,
    "total_pnl_pct": 1.308,
    "sharpe": 0.66,
    "max_dd_pct": 1.97,
    "by_symbol": {
        "BTCUSDT":  {"trades": 10, "wr": 40.0, "pnl": 0.824},
        "ETHUSDT":  {"trades": 8,  "wr": 25.0, "pnl": -0.086},
        "XRPUSDT":  {"trades": 11, "wr": 36.4, "pnl": 0.216},
        "LINKUSDT": {"trades": 13, "wr": 38.5, "pnl": -0.380},
        "SOLUSDT":  {"trades": 10, "wr": 30.0, "pnl": 0.735},
    }
}


def fetch(sym, start_dt, end_dt, gran="4H"):
    all_c = []
    et = int(end_dt.timestamp() * 1000)
    st = int(start_dt.timestamp() * 1000)
    ce = et
    for _ in range(15):
        # Don't use startTime — Bitget may ignore it for older data
        p = {"symbol": sym, "granularity": gran, "limit": 1000,
             "endTime": ce, "productType": "USDT-FUTURES"}
        r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=p, timeout=30).json()
        if r.get("code") != "00000" or not r.get("data"):
            break
        for c in r["data"]:
            ts = int(c[0])
            if ts >= st:  # filter by start date manually
                all_c.append([ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        # Check if we've gone past start date
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
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    return wins / len(trades) * 100


# ── Load data ──
print("=" * 100)
print("P3 OUT-OF-SAMPLE VALIDATION")
print(f"OOS period: {OOS_START.date()} -> {OOS_END.date()}")
print(f"Train period: 2026-01-12 -> 2026-04-12")
print("=" * 100)
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

# ── Run backtest ──
print()
print("-" * 100)
header = f"{'Symbol':<10} {'Trades':>6} {'WR%':>6} {'PnL%':>8} {'MaxDD%':>8} {'Sharpe':>7}"
print(header)
print("-" * 100)

total_trades = 0
total_pnl = 0.0
all_trades = []
oos_results = {}

for s in SYMBOLS:
    df = data[s]
    if df.empty or len(df) < 100:
        print(f"  {s}: SKIP (insufficient data: {len(df)} candles)")
        continue
    bt = Backtester(initial_balance=10000.0)
    res = bt.run(df=df, symbol=s, btc_df=btc_df, btc_1d_df=btc_1d)
    t = res.total_trades
    wr = (res.winning_trades / t * 100) if t > 0 else 0
    pnl = (res.final_balance - 10000) / 100
    dd = res.max_drawdown_pct
    sym_sharpe = compute_sharpe(res.trades)
    oos_results[s] = {"trades": t, "wr": round(wr, 1), "pnl": round(pnl, 3),
                      "max_dd": round(dd, 2), "sharpe": round(sym_sharpe, 2)}
    all_trades.extend(res.trades)
    print(f"{s:<10} {t:>6} {wr:>6.1f} {pnl:>8.3f} {dd:>8.2f} {sym_sharpe:>7.2f}")
    total_trades += t
    total_pnl += pnl

print("-" * 100)
portfolio_sharpe = compute_sharpe(all_trades)
portfolio_dd = compute_max_dd(all_trades)
portfolio_wr = compute_win_rate(all_trades)
print(f"{'TOTAL':<10} {total_trades:>6} {portfolio_wr:>6.1f} {total_pnl:>8.3f} {portfolio_dd:>8.2f} {portfolio_sharpe:>7.2f}")

# ── Comparison table ──
print()
print("=" * 100)
print("COMPARISON: Train vs OOS")
print("=" * 100)

train = TRAIN_RESULTS
train_wr = sum(1 for s in train["by_symbol"].values() if s["wr"] > 0) / len(train["by_symbol"]) * 100  # approx

# Compute train WR from individual symbols
train_total_wins = 0
train_total_trades = 0
for s, v in train["by_symbol"].items():
    wins = round(v["trades"] * v["wr"] / 100)
    train_total_wins += wins
    train_total_trades += v["trades"]
train_wr_actual = train_total_wins / train_total_trades * 100 if train_total_trades > 0 else 0

comp_header = f"{'Metric':<20} {'Train':>12} {'OOS':>12} {'Delta':>12} {'Status':>10}"
print(comp_header)
print("-" * 100)

metrics = [
    ("Trades",   train["total_trades"],   total_trades,    total_trades - train["total_trades"]),
    ("PnL %",    train["total_pnl_pct"],  total_pnl,       total_pnl - train["total_pnl_pct"]),
    ("Sharpe",   train["sharpe"],          portfolio_sharpe, portfolio_sharpe - train["sharpe"]),
    ("MaxDD %",  train["max_dd_pct"],      portfolio_dd,     portfolio_dd - train["max_dd_pct"]),
    ("WinRate %", train_wr_actual,         portfolio_wr,     portfolio_wr - train_wr_actual),
]

for name, train_val, oos_val, delta in metrics:
    if name == "MaxDD %":
        status = "OK" if oos_val <= train_val * 1.5 else "WARN"
    elif name == "PnL %":
        status = "PASS" if oos_val > 0 else "FAIL"
    elif name == "Sharpe":
        status = "PASS" if oos_val > 0.3 else "WARN" if oos_val > 0 else "FAIL"
    elif name == "WinRate %":
        status = "OK" if oos_val >= 30 else "WARN"
    else:
        status = ""
    print(f"{name:<20} {train_val:>12.3f} {oos_val:>12.3f} {delta:>+12.3f} {status:>10}")

# ── Per-symbol comparison ──
print()
print("Per-symbol comparison:")
print(f"{'Symbol':<10} {'Train PnL%':>10} {'OOS PnL%':>10} {'Delta':>10} {'Train WR%':>10} {'OOS WR%':>10}")
print("-" * 70)
for s in SYMBOLS:
    t_data = train["by_symbol"].get(s, {"pnl": 0, "wr": 0})
    o_data = oos_results.get(s, {"pnl": 0, "wr": 0})
    delta_pnl = o_data["pnl"] - t_data["pnl"]
    print(f"{s:<10} {t_data['pnl']:>10.3f} {o_data['pnl']:>10.3f} {delta_pnl:>+10.3f} {t_data['wr']:>10.1f} {o_data['wr']:>10.1f}")

# ── Decision ──
print()
print("=" * 100)
oos_pnl_pass = total_pnl > 0
oos_sharpe_pass = portfolio_sharpe > 0.3
oos_dd_ok = portfolio_dd <= train["max_dd_pct"] * 1.5

if oos_pnl_pass and oos_sharpe_pass:
    verdict = "PASS"
    print(f">>> OOS VERDICT: PASS")
    print(f"    PnL {total_pnl:.3f}% > 0% and Sharpe {portfolio_sharpe:.2f} > 0.3")
    print(f"    Strategy generalizes. Safe to merge.")
elif oos_pnl_pass:
    verdict = "MARGINAL"
    print(f">>> OOS VERDICT: MARGINAL")
    print(f"    PnL {total_pnl:.3f}% > 0% but Sharpe {portfolio_sharpe:.2f} <= 0.3")
    print(f"    Strategy is profitable but noisy. Consider with caution.")
else:
    verdict = "FAIL"
    print(f">>> OOS VERDICT: FAIL (likely overfitting)")
    print(f"    PnL {total_pnl:.3f}% <= 0%")
    print(f"    P3 parameters may be overfit to train period.")

print("=" * 100)

# ── Save ──
result = {
    "oos_period": f"{OOS_START.date()} -> {OOS_END.date()}",
    "train_period": "2026-01-12 -> 2026-04-12",
    "symbols": SYMBOLS,
    "oos_results": {
        "total_trades": total_trades,
        "total_pnl_pct": round(total_pnl, 3),
        "sharpe": round(portfolio_sharpe, 2),
        "max_dd_pct": round(portfolio_dd, 2),
        "win_rate_pct": round(portfolio_wr, 1),
        "by_symbol": oos_results,
    },
    "train_results": train,
    "comparison": {
        "delta_trades": total_trades - train["total_trades"],
        "delta_pnl_pct": round(total_pnl - train["total_pnl_pct"], 3),
        "delta_sharpe": round(portfolio_sharpe - train["sharpe"], 2),
        "delta_max_dd": round(portfolio_dd - train["max_dd_pct"], 2),
    },
    "verdict": verdict,
}
with open("baseline_p3_oos.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to baseline_p3_oos.json")
