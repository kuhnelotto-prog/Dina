#!/usr/bin/env python3
"""
Quick: BULL LONG-only threshold analysis.
Shows WR, avg MFE, avg MAE for thresholds 0.10, 0.15, 0.20.
Then implements dual-regime backtester and runs Train + OOS for SOL and BTC.
"""
import sys, os, time, logging, json, math
import pandas as pd
import numpy as np
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from backtester import Backtester, BacktestPosition, ADXFilter
from indicators_calc import IndicatorsCalculator

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


def collect_bull_long_signals(df, symbol, btc_df, btc_1d):
    """Collect BULL LONG-only signals with MFE/MAE analysis."""
    calc = IndicatorsCalculator()
    adx_filter = ADXFilter(threshold=18.0)
    weights = {
        "ema_cross": 1.0, "volume_spike": 1.0, "engulfing": 0.8,
        "fvg": 0.6, "macd_cross": 0.5, "rsi_filter": 0.4,
        "bb_squeeze": 0.3, "sweep": 0.7,
    }
    
    btc_ema50 = None
    if symbol == "BTCUSDT":
        btc_ema50 = df['close'].ewm(span=50, adjust=False).mean()
    elif btc_df is not None and len(btc_df) >= 50:
        btc_ema50 = btc_df['close'].ewm(span=50, adjust=False).mean()
    
    signals = []
    for i in range(51, len(df) - 1):
        timestamp = df.index[i]
        current_price = df.iloc[i]['close']
        
        # Check regime
        if btc_ema50 is not None:
            if symbol == "BTCUSDT":
                is_bull = current_price > btc_ema50.iloc[i]
            else:
                try:
                    idx = btc_ema50.index.get_indexer([timestamp], method='nearest')[0]
                    is_bull = btc_df['close'].iloc[idx] > btc_ema50.iloc[idx]
                except:
                    is_bull = False
        else:
            is_bull = False
        
        if not is_bull:
            continue  # Only BULL regime
        
        slice_df = df.iloc[:i+1].copy()
        indicators = calc.compute(slice_df)
        if "error" in indicators:
            continue
        
        adx_ok, _ = adx_filter.check(indicators.get("adx", 0.0))
        if not adx_ok:
            continue
        
        composite = Backtester._compute_composite(indicators, weights)
        is_bullish = indicators["ema_fast"] > indicators["ema_slow"]
        rsi = indicators.get("rsi", 50)
        
        # LONG only
        if composite <= 0 or not is_bullish or rsi >= 70:
            continue
        
        atr_pct = indicators.get("atr_pct", 0)
        sl_pct = 1.5 * atr_pct / 100 if atr_pct > 0.1 else 0.03
        
        next_candle = df.iloc[i + 1]
        entry_price = next_candle['open']
        sl_price = entry_price * (1 - sl_pct)
        R = abs(entry_price - sl_price)
        
        max_favorable = 0
        max_adverse = 0
        hit_sl = False
        
        for j in range(i + 1, min(i + 31, len(df))):
            candle = df.iloc[j]
            favorable = (candle['high'] - entry_price) / R if R > 0 else 0
            adverse = (entry_price - candle['low']) / R if R > 0 else 0
            max_favorable = max(max_favorable, favorable)
            max_adverse = max(max_adverse, adverse)
            if adverse >= 1.0:
                hit_sl = True
                break
        
        signals.append({
            "composite": abs(composite),
            "mfe": max_favorable,
            "mae": max_adverse,
            "profitable": max_favorable > max_adverse,
            "hit_sl": hit_sl,
        })
    
    return signals


# ══════════════════════════════════════════════════════════════
# PART 1: BULL LONG threshold analysis
# ══════════════════════════════════════════════════════════════
print("=" * 100)
print("PART 1: BULL LONG-only threshold analysis (Train period)")
print("=" * 100)

btc_df_train = fetch("BTCUSDT", TRAIN_START, TRAIN_END)
btc_1d = fetch1d("BTCUSDT", limit=300)

all_bull_longs = []
for sym in ["BTCUSDT", "ETHUSDT", "XRPUSDT", "LINKUSDT", "SOLUSDT"]:
    df = btc_df_train if sym == "BTCUSDT" else fetch(sym, TRAIN_START, TRAIN_END)
    sigs = collect_bull_long_signals(df, sym, btc_df_train, btc_1d)
    all_bull_longs.extend(sigs)
    time.sleep(0.2)

print(f"\nTotal BULL LONG signals: {len(all_bull_longs)}")
print()
print(f"{'Threshold':>10} {'Signals':>8} {'WR%':>6} {'Avg MFE':>8} {'Avg MAE':>8} {'MFE/MAE':>8} {'SL Hit%':>8}")
print("-" * 65)

threshold_results = {}
for thresh in [0.05, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30]:
    filtered = [s for s in all_bull_longs if s["composite"] >= thresh]
    if not filtered:
        continue
    p = sum(1 for s in filtered if s["profitable"])
    wr = p / len(filtered) * 100
    avg_mfe = sum(s["mfe"] for s in filtered) / len(filtered)
    avg_mae = sum(s["mae"] for s in filtered) / len(filtered)
    ratio = avg_mfe / avg_mae if avg_mae > 0 else 999
    sl_pct = sum(1 for s in filtered if s["hit_sl"]) / len(filtered) * 100
    
    print(f"{thresh:>10.2f} {len(filtered):>8} {wr:>6.1f} {avg_mfe:>8.2f}R {avg_mae:>8.2f}R {ratio:>8.2f} {sl_pct:>8.1f}")
    threshold_results[str(thresh)] = {
        "signals": len(filtered), "wr": round(wr, 1),
        "avg_mfe": round(avg_mfe, 2), "avg_mae": round(avg_mae, 2),
        "mfe_mae_ratio": round(ratio, 2), "sl_hit_pct": round(sl_pct, 1),
    }

# ══════════════════════════════════════════════════════════════
# PART 2: Dual-regime backtester
# ══════════════════════════════════════════════════════════════
print()
print("=" * 100)
print("PART 2: Dual-regime backtester")
print("=" * 100)


def run_dual_regime(df, symbol, btc_df, btc_1d, bull_threshold=0.15):
    """
    Dual-regime backtester:
    BULL (BTC > EMA50 1D): LONG only, TP=1.5R, tighter trailing
    BEAR (BTC < EMA50 1D): Long+Short, TP=2.5R, current trailing
    """
    calc = IndicatorsCalculator()
    adx_filter = ADXFilter(threshold=18.0)
    weights = {
        "ema_cross": 1.0, "volume_spike": 1.0, "engulfing": 0.8,
        "fvg": 0.6, "macd_cross": 0.5, "rsi_filter": 0.4,
        "bb_squeeze": 0.3, "sweep": 0.7,
    }
    
    # BTC EMA50 4H for regime
    btc_ema50_4h = None
    if symbol == "BTCUSDT":
        btc_ema50_4h = df['close'].ewm(span=50, adjust=False).mean()
    elif btc_df is not None and len(btc_df) >= 50:
        btc_ema50_4h = btc_df['close'].ewm(span=50, adjust=False).mean()
    
    # BTC 1D EMA50 for master filter
    btc_1d_ema50 = None
    if btc_1d is not None and len(btc_1d) >= 50:
        btc_1d_ema50 = btc_1d['close'].ewm(span=50, adjust=False).mean()
    
    # BEAR thresholds (current)
    BEAR_LONG_BULL = 0.20
    BEAR_LONG_BEAR = 0.30
    BEAR_SHORT_BULL = 0.35
    BEAR_SHORT_BEAR = 0.35
    
    balance = 10000.0
    position = None
    pending = None
    all_trades = []
    bull_trades = []
    bear_trades = []
    
    for i, (timestamp, row) in enumerate(df.iterrows()):
        current_price = row['close']
        
        # Determine regime (BTC EMA50 4H)
        regime = "BEAR"
        if btc_ema50_4h is not None:
            if symbol == "BTCUSDT":
                regime = "BULL" if current_price > btc_ema50_4h.iloc[i] else "BEAR"
            else:
                try:
                    idx = btc_ema50_4h.index.get_indexer([timestamp], method='nearest')[0]
                    regime = "BULL" if btc_df['close'].iloc[idx] > btc_ema50_4h.iloc[idx] else "BEAR"
                except:
                    regime = "BEAR"
        
        # Execute pending signal
        if pending is not None and position is None:
            sig = pending
            pending = None
            entry_price = row['open']
            
            if sig["side"] == "long":
                sl_price = entry_price * (1 - sig["sl_pct"])
                tp_price = entry_price * (1 + sig["tp_pct"])
            else:
                sl_price = entry_price * (1 + sig["sl_pct"])
                tp_price = entry_price * (1 - sig["tp_pct"])
            
            position = BacktestPosition(
                symbol=symbol, side=sig["side"], entry_price=entry_price,
                size_usd=balance * 0.1, sl_price=sl_price, tp_price=tp_price,
                timestamp=timestamp
            )
            position._regime = sig["regime"]
            
            # Override trailing for BULL: tighter TP
            if sig["regime"] == "BULL":
                position._bull_mode = True
            else:
                position._bull_mode = False
        else:
            pending = None
        
        # Update position
        if position is not None:
            # For BULL mode: check 1.5R TP manually
            if getattr(position, '_bull_mode', False) and not position.is_closed:
                R = position.initial_risk
                if R > 0:
                    if position.side == "long":
                        r_mult = (row['high'] - position.entry_price) / R
                    else:
                        r_mult = (position.entry_price - row['low']) / R
                    
                    if r_mult >= 1.5:
                        # Close at 1.5R
                        if position.side == "long":
                            exit_p = position.entry_price + R * 1.5
                        else:
                            exit_p = position.entry_price - R * 1.5
                        position._close(exit_p, "BULL_TP_1.5R", timestamp=timestamp)
            
            if not position.is_closed:
                closed, _ = position.update(current_price, high=row['high'], low=row['low'], timestamp=timestamp)
            else:
                closed = True
            
            if position.is_closed:
                all_trades.append(position)
                balance += position.pnl_usd
                if getattr(position, '_regime', 'BEAR') == "BULL":
                    bull_trades.append(position)
                else:
                    bear_trades.append(position)
                position = None
        
        # Generate signals
        if position is None and i >= 50:
            slice_df = df.iloc[:i+1].copy()
            indicators = calc.compute(slice_df)
            if "error" in indicators:
                continue
            
            adx_ok, _ = adx_filter.check(indicators.get("adx", 0.0))
            if not adx_ok:
                continue
            
            composite = Backtester._compute_composite(indicators, weights)
            is_bullish = indicators["ema_fast"] > indicators["ema_slow"]
            rsi = indicators.get("rsi", 50)
            atr_pct = indicators.get("atr_pct", 0)
            sl_pct = 1.5 * atr_pct / 100 if atr_pct > 0.1 else 0.03
            
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
                except:
                    pass
            
            if regime == "BULL":
                # BULL: LONG only, lower threshold, TP 1.5R
                tp_pct = 1.5 * atr_pct / 100 if atr_pct > 0.1 else 0.02  # 1.5R TP
                if composite > bull_threshold and is_bullish and rsi < 70 and btc_1d_allows_long:
                    pending = {"side": "long", "sl_pct": sl_pct, "tp_pct": tp_pct,
                              "composite": composite, "regime": "BULL"}
            else:
                # BEAR: current logic
                tp_pct = 3.0 * atr_pct / 100 if atr_pct > 0.1 else 0.05  # 2.5R via trailing
                btc_regime_4h = regime
                threshold_long = BEAR_LONG_BULL if btc_regime_4h == "BULL" else BEAR_LONG_BEAR
                threshold_short = BEAR_SHORT_BEAR if btc_regime_4h == "BEAR" else BEAR_SHORT_BULL
                
                if composite > threshold_long and is_bullish and rsi < 70 and btc_1d_allows_long:
                    pending = {"side": "long", "sl_pct": sl_pct, "tp_pct": tp_pct,
                              "composite": composite, "regime": "BEAR"}
                elif composite < -threshold_short and not is_bullish and rsi > 30 and btc_1d_allows_short:
                    pending = {"side": "short", "sl_pct": sl_pct, "tp_pct": tp_pct,
                              "composite": composite, "regime": "BEAR"}
    
    # Close remaining
    if position is not None:
        last_ts = df.index[-1]
        position._close(df.iloc[-1]['close'], "END_OF_BACKTEST", timestamp=last_ts)
        all_trades.append(position)
        if getattr(position, '_regime', 'BEAR') == "BULL":
            bull_trades.append(position)
        else:
            bear_trades.append(position)
    
    return all_trades, bull_trades, bear_trades


def print_trade_summary(trades, label, initial_balance=10000.0):
    if not trades:
        print(f"  {label}: 0 trades")
        return {"trades": 0, "pnl": 0, "wr": 0, "sharpe": 0}
    total_pnl = sum(t.pnl_usd for t in trades)
    pnl_pct = total_pnl / initial_balance * 100
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    wr = wins / len(trades) * 100
    sh = compute_sharpe(trades, initial_balance)
    print(f"  {label}: {len(trades)} trades, WR {wr:.1f}%, PnL {pnl_pct:+.3f}%, Sharpe {sh:.2f}")
    return {"trades": len(trades), "pnl": round(pnl_pct, 3), "wr": round(wr, 1), "sharpe": round(sh, 2)}


# Run for SOL and BTC on Train and OOS
test_symbols = ["SOLUSDT", "BTCUSDT"]
test_thresholds = [0.10, 0.15, 0.20]

results = {}

for period_name, (start, end) in [("Train", (TRAIN_START, TRAIN_END)), ("OOS", (OOS_START, OOS_END))]:
    print(f"\n{'='*100}")
    print(f"{period_name} period: {start.date()} -> {end.date()}")
    print(f"{'='*100}")
    
    btc_df = fetch("BTCUSDT", start, end)
    btc_1d_data = fetch1d("BTCUSDT", limit=300)
    
    for sym in test_symbols:
        df = btc_df if sym == "BTCUSDT" else fetch(sym, start, end)
        time.sleep(0.2)
        
        for thresh in test_thresholds:
            print(f"\n--- {sym} | bull_threshold={thresh} ---")
            all_t, bull_t, bear_t = run_dual_regime(df, sym, btc_df, btc_1d_data, bull_threshold=thresh)
            
            total = print_trade_summary(all_t, "TOTAL")
            bull = print_trade_summary(bull_t, "  BULL")
            bear = print_trade_summary(bear_t, "  BEAR")
            
            key = f"{period_name}_{sym}_{thresh}"
            results[key] = {"total": total, "bull": bull, "bear": bear}
        
        # Also run baseline (current strategy, no dual-regime)
        print(f"\n--- {sym} | BASELINE (current, no dual-regime) ---")
        bt = Backtester(initial_balance=10000.0)
        res = bt.run(df=df, symbol=sym, btc_df=btc_df, btc_1d_df=btc_1d_data)
        t = res.total_trades
        wr = (res.winning_trades / t * 100) if t > 0 else 0
        pnl = (res.final_balance - 10000) / 100
        sh = compute_sharpe(res.trades)
        print(f"  BASELINE: {t} trades, WR {wr:.1f}%, PnL {pnl:+.3f}%, Sharpe {sh:.2f}")
        results[f"{period_name}_{sym}_baseline"] = {
            "total": {"trades": t, "pnl": round(pnl, 3), "wr": round(wr, 1), "sharpe": round(sh, 2)}
        }

# Save
with open("dual_regime_results.json", "w") as f:
    json.dump({"threshold_analysis": threshold_results, "dual_regime": results}, f, indent=2)
print(f"\nSaved to dual_regime_results.json")
