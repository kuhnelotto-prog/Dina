#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debug_scores.py - Сравнение score-распределений Backtester vs SignalBuilder
на реальных данных BTCUSDT 4H за 30 дней.
"""
import sys, os, time, logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indicators_calc import IndicatorsCalculator
from backtester import Backtester
from signal_builder import SignalBuilder

# ============================================================================
# Fetch real data
# ============================================================================
def fetch_candles(symbol, days=30, granularity="4H"):
    import requests
    all_candles = []
    end_time = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
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
    print("=" * 90)
    print(f"DEBUG SCORES: {symbol} 4H, 30 days")
    print("=" * 90)

    df = fetch_candles(symbol, days=30)
    print(f"Candles: {len(df)}")
    if df.empty or len(df) < 50:
        print("Not enough data")
        return

    calc = IndicatorsCalculator()
    weights = {
        "ema_cross": 1.0, "volume_spike": 1.0, "engulfing": 0.8,
        "fvg": 0.6, "macd_cross": 0.5, "rsi_filter": 0.4,
        "bb_squeeze": 0.3, "sweep": 0.7,
    }

    # ============================================================================
    # 1. Backtester scores (direction-agnostic)
    # ============================================================================
    bt_scores = []
    bt_details = []
    for i in range(30, len(df)):
        slice_df = df.iloc[:i+1].copy()
        indicators = calc.compute(slice_df)
        if "error" in indicators:
            continue
        score = Backtester._compute_composite(indicators, weights)
        bt_scores.append(score)
        bt_details.append({
            "idx": i,
            "price": indicators["price"],
            "ema_fast": indicators["ema_fast"],
            "ema_slow": indicators["ema_slow"],
            "rsi": indicators["rsi"],
            "macd": indicators["macd"],
            "macd_signal": indicators["macd_signal"],
            "bb_upper": indicators["bb_upper"],
            "bb_lower": indicators["bb_lower"],
            "volume_ratio": indicators["volume_ratio"],
            "score": score,
        })

    bt_arr = np.array(bt_scores)
    print(f"\n--- BACKTESTER scores (direction-agnostic) ---")
    print(f"  Count:  {len(bt_arr)}")
    print(f"  Min:    {bt_arr.min():.4f}")
    print(f"  Max:    {bt_arr.max():.4f}")
    print(f"  Mean:   {bt_arr.mean():.4f}")
    print(f"  Median: {np.median(bt_arr):.4f}")
    print(f"  Std:    {bt_arr.std():.4f}")
    print(f"  > 0.35: {(bt_arr > 0.35).sum()} ({(bt_arr > 0.35).sum()/len(bt_arr)*100:.1f}%)")
    print(f"  <-0.35: {(bt_arr < -0.35).sum()} ({(bt_arr < -0.35).sum()/len(bt_arr)*100:.1f}%)")
    print(f"  Signals (LONG+SHORT): {(bt_arr > 0.35).sum() + (bt_arr < -0.35).sum()}")

    # ============================================================================
    # 2. SignalBuilder scores — use SignalBuilder._calculate_composite directly
    # ============================================================================
    sb = SignalBuilder(symbols=[symbol], direction="LONG")
    sb_scores = []

    for i in range(30, len(df)):
        slice_df = df.iloc[:i+1].copy()
        indicators = calc.compute(slice_df)
        if "error" in indicators:
            sb_scores.append(0.0)
            continue
        tf_signal = sb._calculate_signal_from_indicators(indicators, "LONG")
        score = sb._calculate_composite(tf_signal, weights)
        sb_scores.append(score)

    sb_arr = np.array(sb_scores)

    print(f"\n--- SIGNALBUILDER scores (direction-AGNOSTIC, fixed) ---")
    print(f"  Count:  {len(sb_arr)}")
    print(f"  Min:    {sb_arr.min():.4f}")
    print(f"  Max:    {sb_arr.max():.4f}")
    print(f"  Mean:   {sb_arr.mean():.4f}")
    print(f"  Median: {np.median(sb_arr):.4f}")
    print(f"  Std:    {sb_arr.std():.4f}")
    print(f"  > 0.35: {(sb_arr > 0.35).sum()} ({(sb_arr > 0.35).sum()/len(sb_arr)*100:.1f}%)")
    print(f"  <-0.35: {(sb_arr < -0.35).sum()} ({(sb_arr < -0.35).sum()/len(sb_arr)*100:.1f}%)")
    print(f"  Signals: {(sb_arr > 0.35).sum() + (sb_arr < -0.35).sum()}")

    # ============================================================================
    # 3. COMPARISON
    # ============================================================================
    print("\n" + "=" * 90)
    print("COMPARISON: Backtester vs SignalBuilder (both direction-agnostic)")
    print("=" * 90)

    match_count = np.sum(np.abs(bt_arr - sb_arr) < 0.001)
    print(f"  Exact match: {match_count}/{len(bt_arr)} ({match_count/len(bt_arr)*100:.1f}%)")
    print(f"  Max diff:    {np.max(np.abs(bt_arr - sb_arr)):.4f}")
    print(f"  Mean diff:   {np.mean(np.abs(bt_arr - sb_arr)):.4f}")

    # Where do they differ?
    diffs = np.abs(bt_arr - sb_arr)
    diff_mask = diffs > 0.001
    if diff_mask.sum() > 0:
        print(f"\n  Differences found at {diff_mask.sum()} candles:")
        print(f"  Cause: Backtester uses slice_df.reset_index() before calc.compute()")
        print(f"         This changes volume_ratio calculation (rolling window shifts)")

    long_bt = (bt_arr > 0.35).sum()
    short_bt = (bt_arr < -0.35).sum()
    long_sb = (sb_arr > 0.35).sum()
    short_sb = (sb_arr < -0.35).sum()
    print(f"\n  Backtester: {long_bt} LONG + {short_bt} SHORT = {long_bt + short_bt} signals")
    print(f"  SignalBuilder: {long_sb} LONG + {short_sb} SHORT = {long_sb + short_sb} signals")

    # Show sample scores
    print(f"\n--- Sample scores (first 10 candles after warmup) ---")
    print(f"{'idx':>4} {'BT':>8} {'SB':>8} {'diff':>8} {'match':>6}")
    for i in range(min(10, len(bt_scores))):
        bt_s = bt_scores[i]
        sb_s = sb_scores[i]
        diff = abs(bt_s - sb_s)
        match = "YES" if diff < 0.001 else "NO"
        print(f"{i:>4} {bt_s:>8.4f} {sb_s:>8.4f} {diff:>8.4f} {match:>6}")


if __name__ == "__main__":
    main()
