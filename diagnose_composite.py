#!/usr/bin/env python3
"""
Диагностика: почему SignalBuilder._calculate_composite() не достигает ±0.30.
Разбираем STATE и EVENT слои по отдельности.
"""
import os, sys, warnings, logging
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

from signal_builder import SignalBuilder
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
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    
    print(f"Fetching {symbol} 4H candles...")
    df = fetch_candles(symbol, start, end, "4H")
    print(f"Got {len(df)} candles\n")
    
    # Создаём SignalBuilder (SHORT)
    sb = SignalBuilder(symbols=[symbol], timeframes=["15m","1h","4h"],
                       direction="SHORT", bus=None, learning=None)
    calc = IndicatorsCalculator()
    
    # Собираем статистику
    scores = []
    state_scores = []
    event_scores = []
    state_details = []
    
    for i in range(50, len(df)):
        window = df.iloc[:i+1]
        indicators = calc.compute(window)
        if "error" in indicators:
            continue
        
        # Получаем сигналы
        signals = sb._calculate_signal_from_indicators(indicators, "SHORT")
        
        # Вручную считаем STATE и EVENT слои
        d = -1  # SHORT
        
        # STATE слой
        state_score = 0.0
        state_max = 4.0
        
        ema_contrib = 0.0
        if signals.get("ema_bullish"):
            ema_contrib = 1.0 * d
        elif signals.get("ema_bearish"):
            ema_contrib = 1.0 * -d
        state_score += ema_contrib
        
        rsi_contrib = 0.0
        if signals.get("rsi_overbought"):
            rsi_contrib = 1.0 * -d
        elif signals.get("rsi_oversold"):
            rsi_contrib = 1.0 * d
        elif signals.get("rsi_high"):
            rsi_contrib = 0.4 * -d
        elif signals.get("rsi_low"):
            rsi_contrib = 0.4 * d
        state_score += rsi_contrib
        
        macd_contrib = 0.0
        if signals.get("macd_bearish"):
            macd_contrib = 1.0 * -d
        elif signals.get("macd_bullish"):
            macd_contrib = 1.0 * d
        state_score += macd_contrib
        
        bb_contrib = 0.0
        if signals.get("bb_above_upper"):
            bb_contrib = 1.0 * -d
        elif signals.get("bb_below_lower"):
            bb_contrib = 1.0 * d
        elif signals.get("bb_squeeze"):
            bb_contrib = 0.3
        state_score += bb_contrib
        
        state_normalized = state_score / state_max
        
        # EVENT слой
        event_score = 0.0
        event_max = 3.1
        
        if signals.get("ema_cross_bull"):
            event_score += 1.0 * d
        elif signals.get("ema_cross_bear"):
            event_score += 1.0 * -d
        if signals.get("engulfing_bull"):
            event_score += 0.8 * d
        elif signals.get("engulfing_bear"):
            event_score += 0.8 * -d
        if signals.get("fvg_bull"):
            event_score += 0.6 * d
        elif signals.get("fvg_bear"):
            event_score += 0.6 * -d
        if signals.get("sweep_bull"):
            event_score += 0.7 * d
        elif signals.get("sweep_bear"):
            event_score += 0.7 * -d
        
        event_normalized = event_score / event_max if event_max > 0 else 0.0
        
        # Composite
        composite = 0.60 * state_normalized + 0.40 * event_normalized
        if signals.get("volume_spike", False):
            composite *= 1.2
        composite = max(-1.0, min(1.0, composite))
        
        # Также получаем composite через SignalBuilder
        sb_composite = sb._calculate_composite(signals, sb._weights)
        
        scores.append(composite)
        state_scores.append(state_normalized)
        event_scores.append(event_normalized)
        state_details.append({
            "ema": ema_contrib,
            "rsi": rsi_contrib,
            "macd": macd_contrib,
            "bb": bb_contrib,
            "state_raw": state_score,
            "state_norm": state_normalized,
            "event_norm": event_normalized,
            "composite": composite,
            "sb_composite": sb_composite,
            "volume_spike": signals.get("volume_spike", False),
        })
    
    scores = np.array(scores)
    state_scores = np.array(state_scores)
    event_scores = np.array(event_scores)
    
    print("=" * 80)
    print(f"ДИАГНОСТИКА COMPOSITE SCORE ({symbol} 4H, {len(scores)} свечей)")
    print("=" * 80)
    
    print(f"\n--- COMPOSITE SCORE ---")
    print(f"  Mean:   {scores.mean():.4f}")
    print(f"  Std:    {scores.std():.4f}")
    print(f"  Min:    {scores.min():.4f}")
    print(f"  Max:    {scores.max():.4f}")
    print(f"  |score| > 0.30: {(np.abs(scores) > 0.30).sum()} ({(np.abs(scores) > 0.30).mean()*100:.1f}%)")
    print(f"  |score| > 0.40: {(np.abs(scores) > 0.40).sum()} ({(np.abs(scores) > 0.40).mean()*100:.1f}%)")
    print(f"  score < -0.30:  {(scores < -0.30).sum()} ({(scores < -0.30).mean()*100:.1f}%)")
    print(f"  score < -0.40:  {(scores < -0.40).sum()} ({(scores < -0.40).mean()*100:.1f}%)")
    print(f"  score == 0:     {(scores == 0).sum()} ({(scores == 0).mean()*100:.1f}%)")
    
    print(f"\n--- STATE SCORE (normalized) ---")
    print(f"  Mean:   {state_scores.mean():.4f}")
    print(f"  Std:    {state_scores.std():.4f}")
    print(f"  Min:    {state_scores.min():.4f}")
    print(f"  Max:    {state_scores.max():.4f}")
    
    print(f"\n--- EVENT SCORE (normalized) ---")
    print(f"  Mean:   {event_scores.mean():.4f}")
    print(f"  Std:    {event_scores.std():.4f}")
    print(f"  Min:    {event_scores.min():.4f}")
    print(f"  Max:    {event_scores.max():.4f}")
    print(f"  Non-zero: {(event_scores != 0).sum()} ({(event_scores != 0).mean()*100:.1f}%)")
    
    # Распределение STATE компонентов
    print(f"\n--- STATE КОМПОНЕНТЫ (d=-1 для SHORT) ---")
    ema_vals = [d["ema"] for d in state_details]
    rsi_vals = [d["rsi"] for d in state_details]
    macd_vals = [d["macd"] for d in state_details]
    bb_vals = [d["bb"] for d in state_details]
    
    for name, vals in [("EMA trend", ema_vals), ("RSI zone", rsi_vals), 
                        ("MACD hist", macd_vals), ("BB position", bb_vals)]:
        vals = np.array(vals)
        pos = (vals > 0).sum()
        neg = (vals < 0).sum()
        zero = (vals == 0).sum()
        print(f"  {name:12s}: positive={pos:3d} ({pos/len(vals)*100:5.1f}%)  "
              f"negative={neg:3d} ({neg/len(vals)*100:5.1f}%)  "
              f"zero={zero:3d} ({zero/len(vals)*100:5.1f}%)  "
              f"mean={vals.mean():+.3f}")
    
    # Проверяем совпадение sb_composite и нашего composite
    sb_composites = np.array([d["sb_composite"] for d in state_details])
    diff = np.abs(scores - sb_composites)
    print(f"\n--- ПРОВЕРКА: sb_composite vs manual ---")
    print(f"  Max diff: {diff.max():.6f}")
    print(f"  Mean diff: {diff.mean():.6f}")
    
    # Гистограмма composite
    print(f"\n--- ГИСТОГРАММА COMPOSITE ---")
    bins = [(-1.0, -0.5), (-0.5, -0.4), (-0.4, -0.3), (-0.3, -0.2), (-0.2, -0.1),
            (-0.1, 0.0), (0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 1.0)]
    for lo, hi in bins:
        count = ((scores >= lo) & (scores < hi)).sum()
        bar = "█" * (count // 2)
        print(f"  [{lo:+.1f}, {hi:+.1f}): {count:4d} {bar}")
    
    # Топ-5 самых сильных SHORT сигналов
    print(f"\n--- ТОП-5 САМЫХ СИЛЬНЫХ SHORT СИГНАЛОВ ---")
    sorted_idx = np.argsort(scores)[:5]
    for idx in sorted_idx:
        d = state_details[idx]
        ts = df.index[idx + 50]
        print(f"  {ts}: composite={d['composite']:+.4f} "
              f"(state={d['state_norm']:+.3f}, event={d['event_norm']:+.3f}) "
              f"ema={d['ema']:+.1f} rsi={d['rsi']:+.1f} macd={d['macd']:+.1f} bb={d['bb']:+.1f} "
              f"vol_spike={d['volume_spike']}")

if __name__ == "__main__":
    main()
