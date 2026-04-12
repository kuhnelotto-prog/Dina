#!/usr/bin/env python3
"""
Regime diagnosis: test all 10 coins in BULL vs BEAR regimes.
Split by BTC EMA50 (4H): BULL = BTC > EMA50, BEAR = BTC < EMA50.
Also test alternative regime filters: EMA200, RSI(14)>50.

Train: 2026-01-12 -> 2026-04-12
OOS:   2025-10-12 -> 2026-01-12
"""
import sys, os, time, logging, json, math
import pandas as pd
import numpy as np
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from backtester import Backtester
from indicators_calc import IndicatorsCalculator

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "LINKUSDT", "SOLUSDT",
           "BNBUSDT", "ADAUSDT", "AVAXUSDT", "ARBUSDT", "DOTUSDT"]

TRAIN_START = datetime(2026, 1, 12)
TRAIN_END = datetime(2026, 4, 12)
OOS_START = datetime(2025, 10, 12)
OOS_END = datetime(2026, 1, 12)


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


def classify_trades_by_regime(trades, btc_df, regime_type="ema50"):
    """Classify each trade as BULL or BEAR based on BTC regime at entry time."""
    if btc_df.empty:
        return {"BULL": trades, "BEAR": []}
    
    btc_close = btc_df['close']
    
    if regime_type == "ema50":
        indicator = btc_close.ewm(span=50, adjust=False).mean()
    elif regime_type == "ema200":
        indicator = btc_close.ewm(span=200, adjust=False).mean()
    elif regime_type == "rsi50":
        # RSI(14) > 50 = BULL
        delta = btc_close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.001)
        indicator = 100 - (100 / (1 + rs))  # This IS the RSI, threshold = 50
    else:
        return {"BULL": trades, "BEAR": []}
    
    bull_trades = []
    bear_trades = []
    
    for t in trades:
        entry_ts = t.entry_time
        try:
            idx = btc_df.index.get_indexer([entry_ts], method='nearest')[0]
            if idx < 0 or idx >= len(indicator):
                bull_trades.append(t)
                continue
            
            if regime_type == "rsi50":
                is_bull = indicator.iloc[idx] > 50
            else:
                is_bull = btc_close.iloc[idx] > indicator.iloc[idx]
            
            if is_bull:
                bull_trades.append(t)
            else:
                bear_trades.append(t)
        except Exception:
            bull_trades.append(t)
    
    return {"BULL": bull_trades, "BEAR": bear_trades}


def run_symbol(sym, df, btc_df, btc_1d):
    """Run backtest for a single symbol, return trades list."""
    if df.empty or len(df) < 100:
        return []
    bt = Backtester(initial_balance=10000.0)
    res = bt.run(df=df, symbol=sym, btc_df=btc_df, btc_1d_df=btc_1d)
    return res.trades


def summarize_trades(trades):
    """Compute summary stats for a list of trades."""
    if not trades:
        return {"trades": 0, "pnl": 0, "wr": 0, "sharpe": 0}
    total_pnl = sum(t.pnl_usd for t in trades) / 100
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    wr = wins / len(trades) * 100
    sh = compute_sharpe(trades)
    return {"trades": len(trades), "pnl": round(total_pnl, 3), "wr": round(wr, 1), "sharpe": round(sh, 2)}


# ── Load data for both periods ──
print("=" * 130)
print("REGIME DIAGNOSIS: BULL vs BEAR across Train and OOS")
print("=" * 130)
print()

periods = {
    "Train": (TRAIN_START, TRAIN_END),
    "OOS": (OOS_START, OOS_END),
}

all_data = {}
btc_dfs = {}
btc_1ds = {}

for period_name, (start, end) in periods.items():
    print(f"Loading {period_name} data ({start.date()} -> {end.date()})...")
    btc_df = fetch("BTCUSDT", start, end)
    btc_1d = fetch1d("BTCUSDT", limit=300)
    btc_dfs[period_name] = btc_df
    btc_1ds[period_name] = btc_1d
    
    data = {}
    for s in SYMBOLS:
        if s == "BTCUSDT":
            data[s] = btc_df
        else:
            data[s] = fetch(s, start, end)
            time.sleep(0.2)
    all_data[period_name] = data
    print(f"  Loaded {len(data)} symbols")

# ── Compute regime stats for BTC ──
for period_name in periods:
    btc_df = btc_dfs[period_name]
    btc_close = btc_df['close']
    ema50 = btc_close.ewm(span=50, adjust=False).mean()
    bull_pct = (btc_close > ema50).sum() / len(btc_close) * 100
    print(f"\n{period_name}: BTC > EMA50 = {bull_pct:.1f}% of candles (BULL regime)")

# ── Run backtests and classify by regime ──
regime_types = ["ema50", "ema200", "rsi50"]
results = {}

for regime_type in regime_types:
    print()
    print("=" * 130)
    print(f"REGIME FILTER: {regime_type.upper()}")
    print("=" * 130)
    
    for period_name in periods:
        print(f"\n--- {period_name} ---")
        print(f"{'Symbol':<10} {'BULL tr':>7} {'BULL PnL%':>10} {'BULL Sh':>8} {'BEAR tr':>7} {'BEAR PnL%':>10} {'BEAR Sh':>8} {'Stable?':>8}")
        print("-" * 80)
        
        btc_df = btc_dfs[period_name]
        btc_1d = btc_1ds[period_name]
        data = all_data[period_name]
        
        for s in SYMBOLS:
            trades = run_symbol(s, data[s], btc_df, btc_1d)
            regime_split = classify_trades_by_regime(trades, btc_df, regime_type)
            
            bull = summarize_trades(regime_split["BULL"])
            bear = summarize_trades(regime_split["BEAR"])
            
            # Stable = profitable in at least one regime with Sharpe > 0.5
            stable = ""
            if bull["pnl"] > 0 and bull["sharpe"] > 0.5:
                stable = "BULL"
            if bear["pnl"] > 0 and bear["sharpe"] > 0.5:
                stable = "BEAR" if not stable else "BOTH"
            if not stable:
                stable = "-"
            
            key = f"{regime_type}_{period_name}_{s}"
            results[key] = {"bull": bull, "bear": bear, "stable": stable}
            
            print(f"{s:<10} {bull['trades']:>7} {bull['pnl']:>+10.3f} {bull['sharpe']:>8.2f} "
                  f"{bear['trades']:>7} {bear['pnl']:>+10.3f} {bear['sharpe']:>8.2f} {stable:>8}")

# ── Cross-period stability analysis ──
print()
print("=" * 130)
print("CROSS-PERIOD STABILITY (coins stable in same regime on BOTH Train and OOS)")
print("=" * 130)

for regime_type in regime_types:
    print(f"\n{regime_type.upper()}:")
    print(f"{'Symbol':<10} {'Train Stable':>12} {'OOS Stable':>10} {'Consistent?':>12}")
    print("-" * 50)
    
    for s in SYMBOLS:
        train_key = f"{regime_type}_Train_{s}"
        oos_key = f"{regime_type}_OOS_{s}"
        train_stable = results.get(train_key, {}).get("stable", "-")
        oos_stable = results.get(oos_key, {}).get("stable", "-")
        
        consistent = "YES" if (train_stable != "-" and oos_stable != "-" and 
                              (train_stable == oos_stable or "BOTH" in [train_stable, oos_stable])) else "-"
        print(f"{s:<10} {train_stable:>12} {oos_stable:>10} {consistent:>12}")

# ── Save ──
with open("regime_diagnosis.json", "w") as f:
    json.dump(results, f, indent=2)
print(f"\nSaved to regime_diagnosis.json")
