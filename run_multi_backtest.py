#!/usr/bin/env python3
"""
MultiSymbol Backtest with max_positions constraint.
10 coins, 3 configs: max_positions = 1, 2, 4
Period: 2026-01-12 -> 2026-04-12, 4H

Capital allocation: total_capital / max_positions per slot.
Priority: by abs(signal_score) — strongest signal gets the slot.
Max 1 position per symbol.
"""
import sys, os, time, logging, json, math
import pandas as pd
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from backtester import Backtester, BacktestPosition, BacktestResult
from indicators_calc import IndicatorsCalculator

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT", "SOLUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]

TRAIN_START = datetime(2026, 1, 12)
TRAIN_END = datetime(2026, 4, 12)
INITIAL_BALANCE = 10000.0


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
    if not trades:
        return 0.0
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


def run_multi_backtest(data_dict, btc_df, btc_1d, max_positions, symbols):
    """
    Run synchronized multi-symbol backtest.
    Iterates candle-by-candle across all symbols simultaneously.
    """
    from backtester import ADXFilter
    
    calc = IndicatorsCalculator()
    adx_filter = ADXFilter(threshold=18.0)
    
    # Weights
    weights = {
        "ema_cross": 1.0, "volume_spike": 1.0, "engulfing": 0.8,
        "fvg": 0.6, "macd_cross": 0.5, "rsi_filter": 0.4,
        "bb_squeeze": 0.3, "sweep": 0.7,
    }
    
    # Thresholds (synced with strategist_client.py)
    THRESHOLD_LONG_BULL = 0.20
    THRESHOLD_LONG_BEAR = 0.30
    THRESHOLD_SHORT_BULL = 0.45   # synced: stricter shorts on bull market
    THRESHOLD_SHORT_BEAR = 0.35   # synced: more aggressive shorts on bear market
    
    # BTC EMA50 for regime
    btc_ema50 = btc_df['close'].ewm(span=50, adjust=False).mean() if len(btc_df) >= 50 else None
    btc_1d_ema50 = btc_1d['close'].ewm(span=50, adjust=False).mean() if len(btc_1d) >= 50 else None
    
    # Get common timestamps
    common_ts = None
    for s in symbols:
        if s in data_dict and not data_dict[s].empty:
            ts = set(data_dict[s].index)
            common_ts = ts if common_ts is None else common_ts & ts
    if not common_ts:
        return [], 0
    common_ts = sorted(common_ts)
    
    balance = INITIAL_BALANCE
    open_positions = {}  # symbol -> BacktestPosition
    pending_signals = {}  # symbol -> signal dict
    all_trades = []
    slot_size = INITIAL_BALANCE / max_positions
    
    for i, timestamp in enumerate(common_ts):
        # ── Execute pending signals (from previous candle) ──
        if pending_signals:
            # Sort by abs(score) descending — strongest signal first
            sorted_pending = sorted(pending_signals.items(), key=lambda x: abs(x[1]["composite"]), reverse=True)
            for sym, sig in sorted_pending:
                if sym in open_positions:
                    continue  # already have position
                if len(open_positions) >= max_positions:
                    break  # no slots
                
                candle = data_dict[sym].loc[timestamp]
                entry_price = candle['open']
                
                if sig["side"] == "long":
                    sl_price = entry_price * (1 - sig["sl_pct"])
                    tp_price = entry_price * (1 + sig["tp_pct"])
                else:
                    sl_price = entry_price * (1 + sig["sl_pct"])
                    tp_price = entry_price * (1 - sig["tp_pct"])
                
                position = BacktestPosition(
                    symbol=sym, side=sig["side"], entry_price=entry_price,
                    size_usd=slot_size, sl_price=sl_price, tp_price=tp_price,
                    timestamp=timestamp
                )
                open_positions[sym] = position
            
            pending_signals = {}
        
        # ── Update open positions ──
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            candle = data_dict[sym].loc[timestamp]
            closed, _ = pos.update(candle['close'], high=candle['high'], low=candle['low'], timestamp=timestamp)
            if closed:
                del open_positions[sym]
                all_trades.append(pos)
                balance += pos.pnl_usd
        
        # ── Generate signals (need 50+ candles) ──
        if i >= 50:
            for sym in symbols:
                if sym in open_positions:
                    continue  # already have position
                if len(open_positions) >= max_positions and sym not in pending_signals:
                    continue  # no slots available
                
                df = data_dict[sym]
                idx = df.index.get_loc(timestamp)
                if idx < 50:
                    continue
                
                slice_df = df.iloc[:idx+1].copy()
                indicators = calc.compute(slice_df)
                if "error" in indicators:
                    continue
                
                # ADX filter
                adx_val = indicators.get("adx", 0.0)
                adx_ok, _ = adx_filter.check(adx_val)
                if not adx_ok:
                    continue
                
                # Composite score
                composite = Backtester._compute_composite(indicators, weights)
                
                # BTC regime
                if btc_ema50 is not None:
                    try:
                        btc_idx = btc_ema50.index.get_indexer([timestamp], method='nearest')[0]
                        btc_price = btc_df['close'].iloc[btc_idx]
                        btc_regime = "BULL" if btc_price > btc_ema50.iloc[btc_idx] else "BEAR"
                    except Exception:
                        btc_regime = "BULL"
                else:
                    btc_regime = "BULL"
                
                threshold_long = THRESHOLD_LONG_BULL if btc_regime == "BULL" else THRESHOLD_LONG_BEAR
                threshold_short = THRESHOLD_SHORT_BEAR if btc_regime == "BEAR" else THRESHOLD_SHORT_BULL
                
                is_bullish = indicators["ema_fast"] > indicators["ema_slow"]
                rsi = indicators.get("rsi", 50)
                
                # BTC 1D master filter
                btc_1d_allows_long = True
                btc_1d_allows_short = True
                if btc_1d_ema50 is not None:
                    try:
                        idx_1d = btc_1d_ema50.index.get_indexer([timestamp], method='pad')[0]
                        if idx_1d >= 0:
                            btc_1d_close = btc_1d['close'].iloc[idx_1d]
                            btc_1d_allows_long = btc_1d_close > btc_1d_ema50.iloc[idx_1d]
                            btc_1d_allows_short = btc_1d_close < btc_1d_ema50.iloc[idx_1d]
                    except Exception:
                        pass
                
                # ATR-based SL/TP
                atr_pct = indicators.get("atr_pct", 0)
                sl_pct = 1.5 * atr_pct / 100 if atr_pct > 0.1 else 0.03
                tp_pct = 3.0 * atr_pct / 100 if atr_pct > 0.1 else 0.05
                
                # LONG signal
                if composite > threshold_long and is_bullish and rsi < 70 and btc_1d_allows_long:
                    pending_signals[sym] = {"side": "long", "sl_pct": sl_pct, "tp_pct": tp_pct, "composite": composite}
                # SHORT signal
                elif composite < -threshold_short and not is_bullish and rsi > 30 and btc_1d_allows_short:
                    pending_signals[sym] = {"side": "short", "sl_pct": sl_pct, "tp_pct": tp_pct, "composite": composite}
    
    # Close remaining positions
    for sym, pos in open_positions.items():
        last_ts = common_ts[-1]
        last_price = data_dict[sym].loc[last_ts]['close']
        pos._close(last_price, "END_OF_BACKTEST", timestamp=last_ts)
        all_trades.append(pos)
    
    return all_trades, len(common_ts)


# ── Load data ──
print("=" * 120)
print("MULTI-SYMBOL BACKTEST (10 coins, max_positions = 1/2/4)")
print(f"Period: {TRAIN_START.date()} -> {TRAIN_END.date()}")
print("=" * 120)
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

# ── Run 3 configs ──
results = {}
for max_pos in [1, 2, 4]:
    print()
    print("=" * 120)
    label = {1: "A (sanity check)", 2: "B", 4: "C"}[max_pos]
    print(f"CONFIG {label}: max_positions = {max_pos}")
    print("=" * 120)
    
    trades, n_candles = run_multi_backtest(data, btc_df, btc_1d, max_pos, SYMBOLS)
    
    total_pnl = sum(t.pnl_usd for t in trades)
    total_pnl_pct = total_pnl / INITIAL_BALANCE * 100
    total_trades = len(trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    wr = wins / total_trades * 100 if total_trades > 0 else 0
    sharpe = compute_sharpe(trades)
    max_dd = compute_max_dd(trades)
    
    print(f"  Candles processed: {n_candles}")
    print(f"  Total trades: {total_trades}")
    print(f"  WinRate: {wr:.1f}%")
    print(f"  PnL: {total_pnl_pct:+.3f}%")
    print(f"  Sharpe: {sharpe:.2f}")
    print(f"  MaxDD: {max_dd:.2f}%")
    
    # Per-symbol breakdown
    sym_stats = {}
    for t in trades:
        if t.symbol not in sym_stats:
            sym_stats[t.symbol] = {"trades": 0, "pnl": 0, "wins": 0}
        sym_stats[t.symbol]["trades"] += 1
        sym_stats[t.symbol]["pnl"] += t.pnl_usd / 100
        if t.pnl_usd > 0:
            sym_stats[t.symbol]["wins"] += 1
    
    print(f"\n  {'Symbol':<10} {'Trades':>6} {'WR%':>6} {'PnL%':>8}")
    print(f"  {'-'*35}")
    for s in SYMBOLS:
        if s in sym_stats:
            st = sym_stats[s]
            wr_s = st["wins"] / st["trades"] * 100 if st["trades"] > 0 else 0
            print(f"  {s:<10} {st['trades']:>6} {wr_s:>6.1f} {st['pnl']:>+8.3f}")
    
    results[f"max{max_pos}"] = {
        "max_positions": max_pos,
        "total_trades": total_trades,
        "total_pnl_pct": round(total_pnl_pct, 3),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(max_dd, 2),
        "win_rate_pct": round(wr, 1),
        "per_symbol": {s: {"trades": st["trades"], "pnl": round(st["pnl"], 3),
                          "wr": round(st["wins"]/st["trades"]*100 if st["trades"]>0 else 0, 1)}
                      for s, st in sym_stats.items()},
    }

# ── Save ──
output = {
    "period": f"{TRAIN_START.date()} -> {TRAIN_END.date()}",
    "symbols": SYMBOLS,
    "initial_balance": INITIAL_BALANCE,
    "configs": results,
}
with open("multi_backtest_results.json", "w") as f:
    json.dump(output, f, indent=2)
print(f"\nSaved to multi_backtest_results.json")
