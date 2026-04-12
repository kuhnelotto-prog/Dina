#!/usr/bin/env python3
"""
Диагностика: почему composite_score всегда ~0.
Проходим по каждой свече BTCUSDT 4H за 90 дней и выводим raw сигналы.
"""
import os, sys, logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from collections import Counter

logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from indicators_calc import IndicatorsCalculator

def fetch_candles(symbol, start_date, end_date, granularity="4H"):
    import requests, time
    all_candles = []
    end_time = int(end_date.timestamp() * 1000)
    start_time = int(start_date.timestamp() * 1000)
    current_end = end_time
    for _ in range(10):
        params = {"symbol": symbol, "granularity": granularity, "limit": 1000,
                  "endTime": current_end, "startTime": start_time, "productType": "umcbl"}
        resp = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=30)
        data = resp.json()
        if data.get("code") != "00000" or not data.get("data"):
            break
        candles = data["data"]
        for c in candles:
            all_candles.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        if len(candles) < 1000:
            break
        earliest = int(candles[-1][0])
        if earliest >= current_end:
            break
        current_end = earliest - 1
        time.sleep(0.1)
    if not all_candles:
        return pd.DataFrame()
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

def main():
    symbol = "BTCUSDT"
    start = datetime.now(timezone.utc) - timedelta(days=90)
    end = datetime.now(timezone.utc)
    
    print(f"Fetching {symbol} 4H candles...")
    df = fetch_candles(symbol, start, end, "4H")
    print(f"Got {len(df)} candles")
    
    calc = IndicatorsCalculator()
    
    # Counters
    signal_counts = Counter()
    composite_values = []
    nonzero_composites = []
    
    # Simulate signal generation for each candle window
    min_window = 50
    
    for i in range(min_window, len(df)):
        window = df.iloc[:i+1]
        indicators = calc.compute(window)
        if "error" in indicators:
            continue
        
        # Check each signal (same logic as _calculate_signal_from_indicators)
        ema_cross_bull = (indicators["ema_fast"] > indicators["ema_slow"]) and \
                         (indicators.get("ema_fast_prev", 0) <= indicators.get("ema_slow_prev", 0))
        ema_cross_bear = (indicators["ema_fast"] < indicators["ema_slow"]) and \
                         (indicators.get("ema_fast_prev", 0) >= indicators.get("ema_slow_prev", 0))
        
        volume_spike = indicators.get("volume_ratio", 1.0) > 1.2
        engulfing_bull = indicators.get("engulfing_bull", False)
        engulfing_bear = indicators.get("engulfing_bear", False)
        fvg_bull = indicators.get("fvg_bull", False)
        fvg_bear = indicators.get("fvg_bear", False)
        sweep_bull = indicators.get("sweep_bull", False)
        sweep_bear = indicators.get("sweep_bear", False)
        rsi = indicators.get("rsi", 50)
        rsi_extreme = rsi < 30 or rsi > 70
        bb_upper = indicators.get("bb_upper", 0)
        bb_lower = indicators.get("bb_lower", 0)
        bb_middle = indicators.get("bb_middle", 1)
        bb_squeeze = (bb_upper - bb_lower) / bb_middle < 0.05 if bb_middle > 0 else False
        
        # EMA state (not cross)
        ema_bullish = indicators["ema_fast"] > indicators["ema_slow"]
        ema_bearish = indicators["ema_fast"] < indicators["ema_slow"]
        
        # Count signals
        if ema_cross_bull: signal_counts["ema_cross_bull"] += 1
        if ema_cross_bear: signal_counts["ema_cross_bear"] += 1
        if volume_spike: signal_counts["volume_spike"] += 1
        if engulfing_bull: signal_counts["engulfing_bull"] += 1
        if engulfing_bear: signal_counts["engulfing_bear"] += 1
        if fvg_bull: signal_counts["fvg_bull"] += 1
        if fvg_bear: signal_counts["fvg_bear"] += 1
        if sweep_bull: signal_counts["sweep_bull"] += 1
        if sweep_bear: signal_counts["sweep_bear"] += 1
        if rsi < 30: signal_counts["rsi_oversold"] += 1
        if rsi > 70: signal_counts["rsi_overbought"] += 1
        if bb_squeeze: signal_counts["bb_squeeze"] += 1
        if ema_bullish: signal_counts["ema_state_bull"] += 1
        if ema_bearish: signal_counts["ema_state_bear"] += 1
        
        # MACD histogram
        macd = indicators.get("macd", 0)
        macd_sig = indicators.get("macd_signal", 0)
        macd_hist = macd - macd_sig
        macd_bearish = macd_hist < 0
        macd_bullish = macd_hist > 0
        if macd_bearish: signal_counts["macd_bearish"] += 1
        if macd_bullish: signal_counts["macd_bullish"] += 1
        
        # BB position
        price = indicators.get("price", 0)
        bb_above = price > bb_upper if bb_upper > 0 else False
        bb_below = price < bb_lower if bb_lower > 0 else False
        if bb_above: signal_counts["bb_above_upper"] += 1
        if bb_below: signal_counts["bb_below_lower"] += 1
        
        # RSI zones
        rsi_high = rsi > 60
        rsi_low = rsi < 40
        if rsi_high: signal_counts["rsi_high_60"] += 1
        if rsi_low: signal_counts["rsi_low_40"] += 1
        
        # Calculate composite v2 (SHORT direction, d=-1)
        d = -1  # SHORT
        
        # STATE layer (60%)
        state_score = 0.0
        state_max = 4.0
        
        if ema_bearish: state_score += 1.0 * -d  # bearish -> +1 for SHORT
        elif ema_bullish: state_score += 1.0 * d  # bullish -> -1 for SHORT
        
        if rsi > 70: state_score += 1.0 * -d      # overbought -> good for short
        elif rsi < 30: state_score += 1.0 * d      # oversold -> bad for short
        elif rsi > 60: state_score += 0.4 * -d
        elif rsi < 40: state_score += 0.4 * d
        
        if macd_bearish: state_score += 1.0 * -d
        elif macd_bullish: state_score += 1.0 * d
        
        if bb_above: state_score += 1.0 * -d
        elif bb_below: state_score += 1.0 * d
        elif bb_squeeze: state_score += 0.3
        
        state_norm = state_score / state_max
        
        # EVENT layer (40%)
        event_score = 0.0
        event_max = 3.2
        
        if ema_cross_bull: event_score += 1.0 * d
        elif ema_cross_bear: event_score += 1.0 * -d
        if engulfing_bull: event_score += 0.8 * d
        elif engulfing_bear: event_score += 0.8 * -d
        if fvg_bull: event_score += 0.6 * d
        elif fvg_bear: event_score += 0.6 * -d
        if sweep_bull: event_score += 0.7 * d
        elif sweep_bear: event_score += 0.7 * -d
        
        event_norm = event_score / event_max
        
        score = 0.60 * state_norm + 0.40 * event_norm
        if volume_spike: score *= 1.2
        score = max(-1.0, min(1.0, score))
        
        composite_values.append(score)
        if abs(score) > 0.01:
            nonzero_composites.append((window.index[-1], score))
    
    total_candles = len(df) - min_window
    
    print(f"\n{'='*80}")
    print(f"ДИАГНОСТИКА СИГНАЛОВ: {symbol} 4H, {total_candles} свечей")
    print(f"{'='*80}")
    
    print(f"\nЧАСТОТА СИГНАЛОВ (из {total_candles} свечей):")
    print(f"{'-'*60}")
    
    # Sort by frequency
    for signal, count in sorted(signal_counts.items(), key=lambda x: -x[1]):
        pct = count / total_candles * 100
        bar = "#" * int(pct)
        print(f"  {signal:<20} {count:>4} ({pct:>5.1f}%) {bar}")
    
    print(f"\nCOMPOSITE SCORE (SHORT direction):")
    print(f"{'-'*60}")
    arr = np.array(composite_values)
    print(f"  Mean:   {arr.mean():+.4f}")
    print(f"  Median: {np.median(arr):+.4f}")
    print(f"  Std:    {arr.std():.4f}")
    print(f"  Min:    {arr.min():+.4f}")
    print(f"  Max:    {arr.max():+.4f}")
    print(f"  Zero:   {sum(1 for v in arr if abs(v) < 0.01)} ({sum(1 for v in arr if abs(v) < 0.01)/len(arr)*100:.1f}%)")
    print(f"  |score| > 0.30: {sum(1 for v in arr if abs(v) > 0.30)} ({sum(1 for v in arr if abs(v) > 0.30)/len(arr)*100:.1f}%)")
    print(f"  score < -0.30:  {sum(1 for v in arr if v < -0.30)} (SHORT signals above threshold)")
    print(f"  score < -0.40:  {sum(1 for v in arr if v < -0.40)} (SHORT signals above old threshold)")
    
    print(f"\nНЕНУЛЕВЫЕ COMPOSITE (последние 20):")
    print(f"{'-'*60}")
    for ts, score in nonzero_composites[-20:]:
        print(f"  {ts}  score={score:+.4f}")
    
    # KEY INSIGHT
    print(f"\n{'='*80}")
    print("КЛЮЧЕВАЯ ПРОБЛЕМА:")
    print(f"{'='*80}")
    print(f"  EMA cross (bull+bear): {signal_counts.get('ema_cross_bull',0) + signal_counts.get('ema_cross_bear',0)} из {total_candles} свечей")
    print(f"  EMA STATE (bull/bear): {signal_counts.get('ema_state_bull',0)}/{signal_counts.get('ema_state_bear',0)}")
    print(f"  Engulfing (bull+bear): {signal_counts.get('engulfing_bull',0) + signal_counts.get('engulfing_bear',0)}")
    print(f"  FVG (bull+bear):       {signal_counts.get('fvg_bull',0) + signal_counts.get('fvg_bear',0)}")
    print(f"  Sweep (bull+bear):     {signal_counts.get('sweep_bull',0) + signal_counts.get('sweep_bear',0)}")
    print(f"  Volume spike:          {signal_counts.get('volume_spike',0)}")
    print(f"  BB squeeze:            {signal_counts.get('bb_squeeze',0)}")
    print()
    print("  -> Все сигналы кроме volume_spike и sweep - ОДНОМОМЕНТНЫЕ СОБЫТИЯ (cross)")
    print("  -> composite_score = 0 в большинстве свечей")
    print("  -> Нужно заменить CROSS на STATE (trending) для базового сигнала")

if __name__ == "__main__":
    main()
