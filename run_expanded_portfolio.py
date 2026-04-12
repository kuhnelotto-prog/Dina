#!/usr/bin/env python3
"""
Expanded portfolio backtest (commit 4ab49be).
10 coins: BTC, ETH, XRP, LINK, SOL, BNB, ADA, AVAX, ARB, DOT
Period: 2026-01-12 -> 2026-04-12 (train), 4H

Two configs:
  A) max_positions = 2 (max 2 open at once, 1 per symbol)
  B) max_positions = 4

Inclusion criteria: PnL > 0% AND Sharpe > 0.2
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

TRAIN_START = datetime(2026, 1, 12)
TRAIN_END = datetime(2026, 4, 12)


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


# ── Load data ──
print("=" * 110)
print("EXPANDED PORTFOLIO BACKTEST (10 coins)")
print(f"Period: {TRAIN_START.date()} -> {TRAIN_END.date()}")
print("=" * 110)
print()
print("Loading data...")

btc_df = fetch("BTCUSDT", TRAIN_START, TRAIN_END)
btc_1d = fetch1d("BTCUSDT", limit=300)
data = {}
for s in SYMBOLS:
    if s == "BTCUSDT":
        data[s] = btc_df
    else:
        data[s] = fetch(s, TRAIN_START, TRAIN_END)
        time.sleep(0.3)
    print(f"  {s}: {len(data[s])} candles")

# ── Phase 1: Individual symbol backtests ──
print()
print("=" * 110)
print("PHASE 1: Individual symbol results (max_positions=1 per symbol)")
print("=" * 110)
print(f"{'Symbol':<10} {'Trades':>6} {'WR%':>6} {'PnL%':>8} {'MaxDD%':>8} {'Sharpe':>7} {'Include?':>10}")
print("-" * 70)

sym_results = {}
for s in SYMBOLS:
    df = data[s]
    if df.empty or len(df) < 100:
        print(f"{s:<10} SKIP ({len(df)} candles)")
        sym_results[s] = {"trades": 0, "wr": 0, "pnl": 0, "max_dd": 0, "sharpe": 0, "include": False, "trade_list": []}
        continue
    bt = Backtester(initial_balance=10000.0)
    res = bt.run(df=df, symbol=s, btc_df=btc_df, btc_1d_df=btc_1d)
    t = res.total_trades
    wr = (res.winning_trades / t * 100) if t > 0 else 0
    pnl = (res.final_balance - 10000) / 100
    dd = res.max_drawdown_pct
    sh = compute_sharpe(res.trades)
    include = bool(pnl > 0 and sh > 0.2)
    sym_results[s] = {"trades": t, "wr": round(wr, 1), "pnl": round(pnl, 3),
                      "max_dd": round(dd, 2), "sharpe": round(sh, 2),
                      "include": include, "trade_list": res.trades}
    tag = "YES" if include else "no"
    print(f"{s:<10} {t:>6} {wr:>6.1f} {pnl:>8.3f} {dd:>8.2f} {sh:>7.2f} {tag:>10}")

# Sort by PnL for top/bottom
sorted_syms = sorted(sym_results.items(), key=lambda x: x[1]["pnl"], reverse=True)
print()
print("Top 3:")
for s, r in sorted_syms[:3]:
    print(f"  {s}: PnL {r['pnl']:+.3f}%, Sharpe {r['sharpe']:.2f}, {r['trades']} trades")
print("Bottom 3:")
for s, r in sorted_syms[-3:]:
    print(f"  {s}: PnL {r['pnl']:+.3f}%, Sharpe {r['sharpe']:.2f}, {r['trades']} trades")

included = [s for s, r in sym_results.items() if r["include"]]
print(f"\nIncluded (PnL>0 & Sharpe>0.2): {included}")

# ── Phase 2: Portfolio simulation with max_positions ──
# We simulate by merging all trades chronologically and applying position limits
def simulate_portfolio(sym_results, max_pos, symbols_list):
    """Simulate portfolio with max_positions constraint."""
    # Collect all trades with their entry/exit times
    all_entries = []
    for s in symbols_list:
        r = sym_results[s]
        for trade in r["trade_list"]:
            all_entries.append({
                "symbol": s,
                "entry_time": trade.entry_time,
                "exit_time": trade.exit_time or trade.entry_time,
                "pnl_usd": trade.pnl_usd,
                "trade": trade,
            })
    
    # Sort by entry time
    all_entries.sort(key=lambda x: x["entry_time"])
    
    # Simulate with position limit
    open_positions = {}  # symbol -> exit_time
    accepted_trades = []
    rejected = 0
    
    for entry in all_entries:
        # Remove expired positions
        open_positions = {s: et for s, et in open_positions.items() 
                         if et > entry["entry_time"]}
        
        # Check constraints
        if entry["symbol"] in open_positions:
            rejected += 1
            continue  # already have position in this symbol
        if len(open_positions) >= max_pos:
            rejected += 1
            continue  # max positions reached
        
        # Accept trade
        open_positions[entry["symbol"]] = entry["exit_time"]
        accepted_trades.append(entry["trade"])
    
    return accepted_trades, rejected


for max_pos in [2, 4]:
    print()
    print("=" * 110)
    print(f"CONFIG {'A' if max_pos == 2 else 'B'}: max_positions = {max_pos} (included symbols only)")
    print("=" * 110)
    
    trades, rejected = simulate_portfolio(sym_results, max_pos, included)
    
    total_pnl = sum(t.pnl_usd for t in trades) / 100  # as % of 10000
    total_trades = len(trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    wr = wins / total_trades * 100 if total_trades > 0 else 0
    sharpe = compute_sharpe(trades)
    max_dd = compute_max_dd(trades)
    
    print(f"  Symbols: {included}")
    print(f"  Trades: {total_trades} (rejected: {rejected})")
    print(f"  WinRate: {wr:.1f}%")
    print(f"  PnL: {total_pnl:+.3f}%")
    print(f"  Sharpe: {sharpe:.2f}")
    print(f"  MaxDD: {max_dd:.2f}%")
    
    # Per-symbol breakdown within accepted trades
    print(f"\n  Per-symbol (accepted trades only):")
    print(f"  {'Symbol':<10} {'Trades':>6} {'PnL%':>8}")
    print(f"  {'-'*30}")
    sym_pnl = {}
    for t in trades:
        if t.symbol not in sym_pnl:
            sym_pnl[t.symbol] = {"trades": 0, "pnl": 0}
        sym_pnl[t.symbol]["trades"] += 1
        sym_pnl[t.symbol]["pnl"] += t.pnl_usd / 100
    for s in included:
        if s in sym_pnl:
            print(f"  {s:<10} {sym_pnl[s]['trades']:>6} {sym_pnl[s]['pnl']:>+8.3f}")

# ── Save results ──
result = {
    "period": f"{TRAIN_START.date()} -> {TRAIN_END.date()}",
    "symbols_tested": SYMBOLS,
    "individual_results": {s: {k: bool(v) if isinstance(v, (bool,)) else v 
                               for k, v in r.items() if k != "trade_list"} 
                          for s, r in sym_results.items()},
    "included_symbols": included,
    "top3": [{"symbol": s, "pnl": r["pnl"], "sharpe": r["sharpe"]} for s, r in sorted_syms[:3]],
    "bottom3": [{"symbol": s, "pnl": r["pnl"], "sharpe": r["sharpe"]} for s, r in sorted_syms[-3:]],
}

# Add portfolio configs
for max_pos in [2, 4]:
    trades, rejected = simulate_portfolio(sym_results, max_pos, included)
    total_pnl = sum(t.pnl_usd for t in trades) / 100
    config_key = f"config_max{max_pos}"
    result[config_key] = {
        "max_positions": max_pos,
        "total_trades": len(trades),
        "rejected_trades": rejected,
        "total_pnl_pct": round(total_pnl, 3),
        "sharpe": round(compute_sharpe(trades), 2),
        "max_dd_pct": round(compute_max_dd(trades), 2),
        "win_rate_pct": round(sum(1 for t in trades if t.pnl_usd > 0) / len(trades) * 100 if trades else 0, 1),
    }

class SafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (bool,)):
            return bool(obj)
        try:
            return float(obj)
        except (TypeError, ValueError):
            return str(obj)

with open("expanded_portfolio_results.json", "w") as f:
    # Convert all values to native Python types
    def sanitize(obj):
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [sanitize(v) for v in obj]
        elif isinstance(obj, bool) or (hasattr(obj, 'item') and isinstance(obj.item(), bool)):
            return bool(obj)
        elif hasattr(obj, 'item'):
            return obj.item()
        return obj
    json.dump(sanitize(result), f, indent=2)
print(f"\nSaved to expanded_portfolio_results.json")
