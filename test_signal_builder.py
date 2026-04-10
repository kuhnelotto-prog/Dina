#!/usr/bin/env python3
"""
Тест SignalBuilder: что возвращает compute() для BTCUSDT.
"""
import os, sys, asyncio, logging
from datetime import datetime, timezone, timedelta
import pandas as pd

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from signal_builder import SignalBuilder

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

async def main():
    symbol = "BTCUSDT"
    start = datetime.now(timezone.utc) - timedelta(days=90)
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    
    print(f"Fetching {symbol} 4H candles...")
    df = fetch_candles(symbol, start, end, "4H")
    print(f"Got {len(df)} candles")
    
    # Создаём SignalBuilder (SHORT direction)
    signal_builder = SignalBuilder(
        symbols=[symbol],
        timeframes=["15m", "1h", "4h"],
        direction="SHORT",
        bus=None,
        learning=None
    )
    
    # Заполняем кэш свечами
    signal_builder._candle_cache[(symbol, "4h")] = df
    
    # Тестируем на последних 10 свечах
    for i in range(len(df)-10, len(df)):
        window = df.iloc[:i+1]
        timestamp = window.index[-1]
        
        signal_builder._candle_cache[(symbol, "4h")] = window
        try:
            signal = await signal_builder.compute(symbol, current_tf="4h")
        except Exception as e:
            print(f"  {timestamp}: ERROR {e}")
            continue
        
        if "error" in signal:
            print(f"  {timestamp}: {signal['error']}")
            continue
        
        composite_score = signal.get("composite_score", 0.0)
        raw_signals = signal.get("raw_signals", {})
        
        print(f"  {timestamp}: composite_score={composite_score:.4f}")
        print(f"    raw_signals: {list(raw_signals.keys())}")
        
        # Проверяем пороги
        threshold_old = 0.40
        threshold_new = 0.30  # BEAR режим (предположим)
        
        if composite_score < -threshold_old:
            print(f"    -> SHORT signal (old threshold {threshold_old})")
        elif composite_score < -threshold_new:
            print(f"    -> SHORT signal (new threshold {threshold_new})")
        
        # Выводим детали
        for key, val in raw_signals.items():
            if isinstance(val, bool) and val:
                print(f"      {key}: {val}")
            elif isinstance(val, (int, float)) and abs(val) > 0.1:
                print(f"      {key}: {val:.2f}")

if __name__ == "__main__":
    asyncio.run(main())