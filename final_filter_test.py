#!/usr/bin/env python3
"""
Финальный тест фильтров с реальными данными
"""
import time
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import warnings
warnings.filterwarnings("ignore")

import sys
sys.path.insert(0, '.')

from signal_builder import SignalBuilder

def fetch_candles(symbol, start_date, end_date, granularity="4H"):
    import requests
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

async def test_symbol(symbol):
    print(f"\n=== {symbol} ===")
    start_date = datetime.now(timezone.utc) - timedelta(days=90)
    end_date = datetime.now(timezone.utc) - timedelta(minutes=5)
    
    df_4h = fetch_candles(symbol, start_date, end_date, "4H")
    if df_4h.empty or len(df_4h) < 100:
        print(f"  Insufficient data")
        return
    
    # Создаём SignalBuilder для LONG и SHORT
    sb_long = SignalBuilder(symbols=[symbol], direction="LONG")
    sb_short = SignalBuilder(symbols=[symbol], direction="SHORT")
    sb_long._signal_cooldown_sec = 0  # отключаем cooldown
    sb_short._signal_cooldown_sec = 0
    
    await sb_long.update_candle(symbol, "4h", df_4h)
    await sb_short.update_candle(symbol, "4h", df_4h)
    
    # Тестируем на последних 20 свечах
    total_signals = 0
    passed_long = 0
    passed_short = 0
    
    for i in range(-20, 0):
        slice_df = df_4h.iloc[:i+1].copy()
        await sb_long.update_candle(symbol, "4h", slice_df)
        await sb_short.update_candle(symbol, "4h", slice_df)
        
        signal_long = await sb_long.compute(symbol, "4h")
        signal_short = await sb_short.compute(symbol, "4h")
        
        if "error" not in signal_long:
            total_signals += 1
            if not signal_long.get("filtered", False):
                passed_long += 1
        
        if "error" not in signal_short:
            total_signals += 1
            if not signal_short.get("filtered", False):
                passed_short += 1
    
    print(f"  Total signals: {total_signals}")
    print(f"  Passed LONG: {passed_long}")
    print(f"  Passed SHORT: {passed_short}")
    print(f"  Total passed: {passed_long + passed_short}")
    
    # Проверяем фильтры на последней свече
    signal = await sb_long.compute(symbol, "4h")
    regime = sb_long.detect_regime(symbol)
    print(f"  Last signal: composite={signal.get('composite_score', 0):.3f}, regime={regime}, filtered={signal.get('filtered', False)}")
    
    return passed_long + passed_short

async def main():
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    
    print("=" * 60)
    print("FINAL FILTER TEST (реальные данные, 90 дней)")
    print("=" * 60)
    
    total_passed = 0
    for symbol in symbols:
        passed = await test_symbol(symbol)
        if passed is not None:
            total_passed += passed
    
    print("\n" + "=" * 60)
    print(f"TOTAL PASSED SIGNALS: {total_passed}")
    print("=" * 60)
    
    if total_passed < 50:
        print(f"⚠️  WARNING: Only {total_passed} signals passed filters")
        print("   Filters may still be too strict")
    else:
        print(f"✅ OK: {total_passed} signals passed filters")
        print("   Filters are reasonable")

if __name__ == "__main__":
    asyncio.run(main())
