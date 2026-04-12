#!/usr/bin/env python3
"""
BULL regime signal analysis for Dina.

Hypothesis: current params (thresholds, trailing, TP) are optimized for BEAR.
BULL needs different entry parameters.

Steps:
1. Collect ALL signals generated during BULL periods (BTC > EMA50)
2. Check win rate — if <30% signal broken, if >40% params need tuning
3. If >40% — find optimal BULL params
4. OOS validate
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

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "LINKUSDT", "SOLUSDT"]

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


def collect_all_signals(df, symbol, btc_df, btc_1d):
    """
    Scan all candles and collect every signal with its score, regime, and outcome.
    Returns list of dicts with signal details.
    """
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
    
    btc_1d_ema50 = None
    if btc_1d is not None and len(btc_1d) >= 50:
        btc_1d_ema50 = btc_1d['close'].ewm(span=50, adjust=False).mean()
    
    signals = []
    
    for i in range(51, len(df) - 1):
        timestamp = df.index[i]
        current_price = df.iloc[i]['close']
        
        slice_df = df.iloc[:i+1].copy()
        indicators = calc.compute(slice_df)
        if "error" in indicators:
            continue
        
        adx_val = indicators.get("adx", 0.0)
        adx_ok, _ = adx_filter.check(adx_val)
        if not adx_ok:
            continue
        
        composite = Backtester._compute_composite(indicators, weights)
        
        # Determine regime
        if btc_ema50 is not None:
            if symbol == "BTCUSDT":
                regime = "BULL" if current_price > btc_ema50.iloc[i] else "BEAR"
            else:
                try:
                    idx = btc_ema50.index.get_indexer([timestamp], method='nearest')[0]
                    btc_price = btc_df['close'].iloc[idx]
                    regime = "BULL" if btc_price > btc_ema50.iloc[idx] else "BEAR"
                except:
                    regime = "UNKNOWN"
        else:
            regime = "UNKNOWN"
        
        # BTC 1D filter
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
        
        is_bullish = indicators["ema_fast"] > indicators["ema_slow"]
        rsi = indicators.get("rsi", 50)
        atr_pct = indicators.get("atr_pct", 0)
        sl_pct = 1.5 * atr_pct / 100 if atr_pct > 0.1 else 0.03
        
        # Check if signal would trigger at various thresholds
        side = None
        if composite > 0 and is_bullish and rsi < 70 and btc_1d_allows_long:
            side = "long"
        elif composite < 0 and not is_bullish and rsi > 30 and btc_1d_allows_short:
            side = "short"
        
        if side is None:
            continue
        
        # Simulate outcome: entry at next candle open, check max favorable/adverse excursion
        next_candle = df.iloc[i + 1]
        entry_price = next_candle['open']
        
        # Look ahead up to 30 candles for outcome
        max_favorable = 0
        max_adverse = 0
        exit_pnl_pct = 0
        sl_price = entry_price * (1 - sl_pct) if side == "long" else entry_price * (1 + sl_pct)
        R = abs(entry_price - sl_price)
        
        hit_sl = False
        hit_1r = False
        hit_2r = False
        hit_25r = False
        candles_to_1r = 0
        
        for j in range(i + 1, min(i + 31, len(df))):
            candle = df.iloc[j]
            if side == "long":
                favorable = (candle['high'] - entry_price) / R if R > 0 else 0
                adverse = (entry_price - candle['low']) / R if R > 0 else 0
                current_r = (candle['close'] - entry_price) / R if R > 0 else 0
            else:
                favorable = (entry_price - candle['low']) / R if R > 0 else 0
                adverse = (candle['high'] - entry_price) / R if R > 0 else 0
                current_r = (entry_price - candle['close']) / R if R > 0 else 0
            
            max_favorable = max(max_favorable, favorable)
            max_adverse = max(max_adverse, adverse)
            
            if favorable >= 1.0 and not hit_1r:
                hit_1r = True
                candles_to_1r = j - i
            if favorable >= 2.0:
                hit_2r = True
            if favorable >= 2.5:
                hit_25r = True
            if adverse >= 1.0:
                hit_sl = True
                break
        
        signals.append({
            "symbol": symbol,
            "timestamp": str(timestamp),
            "regime": regime,
            "side": side,
            "composite": round(composite, 4),
            "abs_composite": round(abs(composite), 4),
            "rsi": round(rsi, 1),
            "atr_pct": round(atr_pct, 2),
            "max_favorable_R": round(max_favorable, 2),
            "max_adverse_R": round(max_adverse, 2),
            "hit_sl": hit_sl,
            "hit_1r": hit_1r,
            "hit_2r": hit_2r,
            "hit_25r": hit_25r,
            "candles_to_1r": candles_to_1r if hit_1r else None,
            "profitable": max_favorable > max_adverse,
        })
    
    return signals


# ── Load data ──
print("=" * 120)
print("BULL REGIME SIGNAL ANALYSIS")
print("=" * 120)

# Load train data
print("\nLoading Train data...")
btc_df_train = fetch("BTCUSDT", TRAIN_START, TRAIN_END)
btc_1d = fetch1d("BTCUSDT", limit=300)
train_data = {}
for s in SYMBOLS:
    train_data[s] = btc_df_train if s == "BTCUSDT" else fetch(s, TRAIN_START, TRAIN_END)
    time.sleep(0.2)

# Collect all signals
print("\nCollecting signals from Train period...")
all_signals = []
for s in SYMBOLS:
    sigs = collect_all_signals(train_data[s], s, btc_df_train, btc_1d)
    all_signals.extend(sigs)
    print(f"  {s}: {len(sigs)} signals")

# ── Step 1: Split by regime ──
bull_signals = [s for s in all_signals if s["regime"] == "BULL"]
bear_signals = [s for s in all_signals if s["regime"] == "BEAR"]

print(f"\nTotal signals: {len(all_signals)}")
print(f"  BULL: {len(bull_signals)}")
print(f"  BEAR: {len(bear_signals)}")

# ── Step 2: Win rates ──
def analyze_signals(signals, label):
    if not signals:
        print(f"\n{label}: No signals")
        return
    
    profitable = sum(1 for s in signals if s["profitable"])
    wr = profitable / len(signals) * 100
    hit_1r = sum(1 for s in signals if s["hit_1r"])
    hit_2r = sum(1 for s in signals if s["hit_2r"])
    hit_25r = sum(1 for s in signals if s["hit_25r"])
    hit_sl = sum(1 for s in signals if s["hit_sl"])
    avg_mfe = sum(s["max_favorable_R"] for s in signals) / len(signals)
    avg_mae = sum(s["max_adverse_R"] for s in signals) / len(signals)
    avg_score = sum(s["abs_composite"] for s in signals) / len(signals)
    
    print(f"\n{label} ({len(signals)} signals):")
    print(f"  Profitable (MFE > MAE): {profitable}/{len(signals)} = {wr:.1f}%")
    print(f"  Hit 1R: {hit_1r}/{len(signals)} = {hit_1r/len(signals)*100:.1f}%")
    print(f"  Hit 2R: {hit_2r}/{len(signals)} = {hit_2r/len(signals)*100:.1f}%")
    print(f"  Hit 2.5R (TP): {hit_25r}/{len(signals)} = {hit_25r/len(signals)*100:.1f}%")
    print(f"  Hit SL: {hit_sl}/{len(signals)} = {hit_sl/len(signals)*100:.1f}%")
    print(f"  Avg MFE: {avg_mfe:.2f}R")
    print(f"  Avg MAE: {avg_mae:.2f}R")
    print(f"  Avg |score|: {avg_score:.4f}")
    
    # By score buckets
    print(f"\n  By score threshold:")
    for thresh in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
        filtered = [s for s in signals if s["abs_composite"] >= thresh]
        if filtered:
            p = sum(1 for s in filtered if s["profitable"])
            h1r = sum(1 for s in filtered if s["hit_1r"])
            h25r = sum(1 for s in filtered if s["hit_25r"])
            print(f"    score >= {thresh:.2f}: {len(filtered):>3} signals, "
                  f"WR {p/len(filtered)*100:>5.1f}%, "
                  f"1R {h1r/len(filtered)*100:>5.1f}%, "
                  f"TP {h25r/len(filtered)*100:>5.1f}%")
    
    # By side
    longs = [s for s in signals if s["side"] == "long"]
    shorts = [s for s in signals if s["side"] == "short"]
    if longs:
        lp = sum(1 for s in longs if s["profitable"])
        print(f"\n  LONG: {len(longs)} signals, WR {lp/len(longs)*100:.1f}%")
    if shorts:
        sp = sum(1 for s in shorts if s["profitable"])
        print(f"  SHORT: {len(shorts)} signals, WR {sp/len(shorts)*100:.1f}%")
    
    return wr

print("\n" + "=" * 120)
print("STEP 2: Signal quality by regime")
print("=" * 120)

bull_wr = analyze_signals(bull_signals, "BULL regime")
bear_wr = analyze_signals(bear_signals, "BEAR regime")

# ── Step 3: If BULL WR > 40%, find optimal params ──
print("\n" + "=" * 120)
print("STEP 3: BULL parameter optimization")
print("=" * 120)

if bull_wr and bull_wr >= 30:
    print(f"\nBULL WR = {bull_wr:.1f}% >= 30% — signals work, optimizing params...")
    
    # Find best threshold for BULL
    best_thresh = 0.20
    best_metric = 0
    for thresh in [0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30]:
        filtered = [s for s in bull_signals if s["abs_composite"] >= thresh]
        if len(filtered) < 3:
            continue
        p = sum(1 for s in filtered if s["profitable"])
        wr = p / len(filtered) * 100
        h1r = sum(1 for s in filtered if s["hit_1r"])
        # Metric: WR * sqrt(trades) — balance quality and quantity
        metric = wr * math.sqrt(len(filtered))
        if metric > best_metric:
            best_metric = metric
            best_thresh = thresh
    
    print(f"  Best BULL threshold: {best_thresh:.2f}")
    
    # Check if wider trailing helps in BULL
    bull_mfe_avg = sum(s["max_favorable_R"] for s in bull_signals) / len(bull_signals) if bull_signals else 0
    bear_mfe_avg = sum(s["max_favorable_R"] for s in bear_signals) / len(bear_signals) if bear_signals else 0
    
    print(f"\n  Avg MFE comparison:")
    print(f"    BULL: {bull_mfe_avg:.2f}R")
    print(f"    BEAR: {bear_mfe_avg:.2f}R")
    
    if bull_mfe_avg > bear_mfe_avg:
        print(f"    BULL moves further ({bull_mfe_avg:.2f}R vs {bear_mfe_avg:.2f}R)")
        print(f"    -> Recommend wider TP: 3.0R instead of 2.5R")
        bull_tp = 3.0
    else:
        print(f"    BULL moves less ({bull_mfe_avg:.2f}R vs {bear_mfe_avg:.2f}R)")
        print(f"    -> Keep TP at 2.5R")
        bull_tp = 2.5
    
    # Check candles to 1R
    bull_1r_candles = [s["candles_to_1r"] for s in bull_signals if s["candles_to_1r"] is not None]
    bear_1r_candles = [s["candles_to_1r"] for s in bear_signals if s["candles_to_1r"] is not None]
    
    if bull_1r_candles:
        avg_bull_1r = sum(bull_1r_candles) / len(bull_1r_candles)
        print(f"\n  Avg candles to 1R:")
        print(f"    BULL: {avg_bull_1r:.1f} candles")
    if bear_1r_candles:
        avg_bear_1r = sum(bear_1r_candles) / len(bear_1r_candles)
        print(f"    BEAR: {avg_bear_1r:.1f} candles")
    
    print(f"\n  RECOMMENDED BULL PARAMS:")
    print(f"    bull_score_threshold: {best_thresh}")
    print(f"    bull_tp_multiplier: {bull_tp}R")
    print(f"    bull_trailing_step1: 0.5R (same)")
    if bull_mfe_avg > 2.0:
        print(f"    bull_trailing_step4: {bull_tp}R (wider)")
    else:
        print(f"    bull_trailing_step4: 2.5R (same)")
    
    bull_params = {
        "threshold": best_thresh,
        "tp_multiplier": bull_tp,
        "trailing_steps": [0.5, 1.0, 1.5, bull_tp],
    }
else:
    print(f"\nBULL WR = {bull_wr:.1f}% < 30% — signals fundamentally broken in BULL")
    print("  -> Consider: only trade in BEAR regime, or use different signal logic for BULL")
    bull_params = None

# ── Save ──
result = {
    "train_period": f"{TRAIN_START.date()} -> {TRAIN_END.date()}",
    "total_signals": len(all_signals),
    "bull_signals": len(bull_signals),
    "bear_signals": len(bear_signals),
    "bull_wr": round(bull_wr, 1) if bull_wr else 0,
    "bear_wr": round(bear_wr, 1) if bear_wr else 0,
    "bull_params": bull_params,
    "signal_details": {
        "bull_by_threshold": {},
        "bear_by_threshold": {},
    }
}

for thresh in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]:
    for regime, sigs in [("bull", bull_signals), ("bear", bear_signals)]:
        filtered = [s for s in sigs if s["abs_composite"] >= thresh]
        if filtered:
            p = sum(1 for s in filtered if s["profitable"])
            result["signal_details"][f"{regime}_by_threshold"][str(thresh)] = {
                "signals": len(filtered),
                "wr": round(p / len(filtered) * 100, 1),
                "hit_1r_pct": round(sum(1 for s in filtered if s["hit_1r"]) / len(filtered) * 100, 1),
                "hit_25r_pct": round(sum(1 for s in filtered if s["hit_25r"]) / len(filtered) * 100, 1),
            }

with open("bull_analysis.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to bull_analysis.json")
