#!/usr/bin/env python3
"""
OOS validation for expanded portfolio (10 coins).
Period: 2025-10-12 -> 2026-01-12, 4H

Focus:
  - ARB OOS validation (was +2.656% on train)
  - ETH/XRP/LINK diagnosis (why negative on train?)
  - All 10 coins for completeness
"""
import sys, os, time, logging, json, math
import pandas as pd
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from backtester import Backtester

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "LINKUSDT", "SOLUSDT",
           "BNBUSDT", "ADAUSDT", "AVAXUSDT", "ARBUSDT", "DOTUSDT"]

OOS_START = datetime(2025, 10, 12)
OOS_END = datetime(2026, 1, 12)

# Train results from expanded_portfolio_results.json
TRAIN = {
    "BTCUSDT":  {"trades": 10, "wr": 40.0, "pnl": 0.824, "sharpe": 2.81},
    "ETHUSDT":  {"trades": 8,  "wr": 25.0, "pnl": -0.086, "sharpe": -0.35},
    "XRPUSDT":  {"trades": 10, "wr": 30.0, "pnl": -0.276, "sharpe": -0.91},
    "LINKUSDT": {"trades": 12, "wr": 33.3, "pnl": -1.028, "sharpe": -2.23},
    "SOLUSDT":  {"trades": 9,  "wr": 22.2, "pnl": 0.552, "sharpe": 1.15},
    "BNBUSDT":  {"trades": 11, "wr": 45.5, "pnl": -0.615, "sharpe": -2.35},
    "ADAUSDT":  {"trades": 13, "wr": 23.1, "pnl": -2.076, "sharpe": -4.72},
    "AVAXUSDT": {"trades": 9,  "wr": 11.1, "pnl": -1.637, "sharpe": -6.50},
    "ARBUSDT":  {"trades": 11, "wr": 63.6, "pnl": 2.656, "sharpe": 4.22},
    "DOTUSDT":  {"trades": 9,  "wr": 44.4, "pnl": 0.057, "sharpe": 0.14},
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


# ── Load OOS data ──
print("=" * 120)
print("OOS VALIDATION: 10 coins (ARB focus + ETH/XRP/LINK diagnosis)")
print(f"OOS period: {OOS_START.date()} -> {OOS_END.date()}")
print("=" * 120)
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

# ── Run OOS backtests ──
print()
print("=" * 120)
print(f"{'Symbol':<10} {'Tr Trades':>9} {'OOS Trades':>10} {'Tr WR%':>7} {'OOS WR%':>8} {'Tr PnL%':>8} {'OOS PnL%':>9} {'Tr Sharpe':>10} {'OOS Sharpe':>11} {'OOS OK?':>8}")
print("-" * 120)

oos_results = {}
for s in SYMBOLS:
    df = data[s]
    tr = TRAIN[s]
    if df.empty or len(df) < 100:
        print(f"{s:<10} SKIP ({len(df)} candles)")
        oos_results[s] = {"trades": 0, "wr": 0, "pnl": 0, "sharpe": 0}
        continue
    bt = Backtester(initial_balance=10000.0)
    res = bt.run(df=df, symbol=s, btc_df=btc_df, btc_1d_df=btc_1d)
    t = res.total_trades
    wr = (res.winning_trades / t * 100) if t > 0 else 0
    pnl = (res.final_balance - 10000) / 100
    sh = compute_sharpe(res.trades)
    oos_results[s] = {"trades": t, "wr": round(wr, 1), "pnl": round(pnl, 3), "sharpe": round(sh, 2)}
    
    ok = "YES" if pnl > 0 and sh > 0.2 else "marginal" if pnl > 0 else "no"
    print(f"{s:<10} {tr['trades']:>9} {t:>10} {tr['wr']:>7.1f} {wr:>8.1f} {tr['pnl']:>+8.3f} {pnl:>+9.3f} {tr['sharpe']:>10.2f} {sh:>11.2f} {ok:>8}")

# ── ARB Focus ──
print()
print("=" * 120)
print("ARB FOCUS:")
arb_tr = TRAIN["ARBUSDT"]
arb_oos = oos_results["ARBUSDT"]
print(f"  Train: {arb_tr['trades']} trades, WR {arb_tr['wr']}%, PnL {arb_tr['pnl']:+.3f}%, Sharpe {arb_tr['sharpe']:.2f}")
print(f"  OOS:   {arb_oos['trades']} trades, WR {arb_oos['wr']}%, PnL {arb_oos['pnl']:+.3f}%, Sharpe {arb_oos['sharpe']:.2f}")
if arb_oos["pnl"] > 0 and arb_oos["sharpe"] > 0.2:
    print("  >>> ARB OOS: PASS — safe to include in portfolio")
elif arb_oos["pnl"] > 0:
    print("  >>> ARB OOS: MARGINAL — profitable but noisy")
else:
    print("  >>> ARB OOS: FAIL — do not include")

# ── ETH/XRP/LINK Diagnosis ──
print()
print("ETH/XRP/LINK DIAGNOSIS:")
for s in ["ETHUSDT", "XRPUSDT", "LINKUSDT"]:
    tr = TRAIN[s]
    oos = oos_results[s]
    delta_pnl = oos["pnl"] - tr["pnl"]
    print(f"  {s}: Train {tr['pnl']:+.3f}% -> OOS {oos['pnl']:+.3f}% (delta {delta_pnl:+.3f}%)")
    if oos["pnl"] > tr["pnl"]:
        print(f"    Improved on OOS — train period was worse, may recover")
    else:
        print(f"    Worse on OOS — consistently unprofitable, exclude")

# ── Summary: which coins to keep ──
print()
print("=" * 120)
print("FINAL PORTFOLIO RECOMMENDATION:")
print(f"{'Symbol':<10} {'Train PnL%':>10} {'OOS PnL%':>10} {'Train Sharpe':>12} {'OOS Sharpe':>11} {'Decision':>10}")
print("-" * 70)
for s in SYMBOLS:
    tr = TRAIN[s]
    oos = oos_results[s]
    # Include if: profitable on BOTH train and OOS, or profitable on OOS with Sharpe > 0.2
    both_positive = tr["pnl"] > 0 and oos["pnl"] > 0
    oos_strong = oos["pnl"] > 0 and oos["sharpe"] > 0.2
    decision = "INCLUDE" if (both_positive or oos_strong) else "EXCLUDE"
    print(f"{s:<10} {tr['pnl']:>+10.3f} {oos['pnl']:>+10.3f} {tr['sharpe']:>12.2f} {oos['sharpe']:>11.2f} {decision:>10}")

included = [s for s in SYMBOLS if (TRAIN[s]["pnl"] > 0 and oos_results[s]["pnl"] > 0) or 
            (oos_results[s]["pnl"] > 0 and oos_results[s]["sharpe"] > 0.2)]
print(f"\nFinal portfolio: {included}")
print("=" * 120)

# ── Save ──
result = {
    "oos_period": f"{OOS_START.date()} -> {OOS_END.date()}",
    "train_period": "2026-01-12 -> 2026-04-12",
    "symbols": SYMBOLS,
    "train_results": {s: {k: v for k, v in d.items()} for s, d in TRAIN.items()},
    "oos_results": oos_results,
    "included_symbols": included,
}
with open("oos_expanded_results.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to oos_expanded_results.json")
