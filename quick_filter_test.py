#!/usr/bin/env python3
"""
Быстрый тест фильтров с отключённым cooldown
"""
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import warnings
warnings.filterwarnings("ignore")

import sys
sys.path.insert(0, '.')

from signal_builder import SignalBuilder

async def test():
    # Создаём тестовые данные (симуляция)
    dates = pd.date_range(start='2026-01-01', periods=100, freq='4h')
    np.random.seed(42)
    df = pd.DataFrame({
        'open': np.random.randn(100).cumsum() + 100,
        'high': np.random.randn(100).cumsum() + 101,
        'low': np.random.randn(100).cumsum() + 99,
        'close': np.random.randn(100).cumsum() + 100,
        'volume': np.random.rand(100) * 1000
    }, index=dates)
    
    # Создаём SignalBuilder с отключённым cooldown
    sb = SignalBuilder(symbols=['TEST'], direction='LONG')
    sb._signal_cooldown_sec = 0  # отключаем cooldown
    
    await sb.update_candle('TEST', '4h', df)
    
    # Тестируем фильтры на последней свече
    signal = await sb.compute('TEST', '4h')
    
    print("Signal result:")
    print(f"  Composite score: {signal.get('composite_score', 0):.3f}")
    print(f"  Filtered: {signal.get('filtered', False)}")
    print(f"  Regime: {sb.detect_regime('TEST')}")
    
    # Проверяем фильтры вручную
    regime = sb.detect_regime('TEST')
    passes = sb._apply_filters(signal, signal.get('composite_score', 0), regime)
    print(f"  Passes filters: {passes}")
    
    # Тестируем разные пороги
    print("\nTesting thresholds:")
    test_scores = [0.1, 0.2, 0.3, 0.4, 0.5]
    for score in test_scores:
        passes = sb._apply_filters(signal, score, regime)
        print(f"  Score {score:.2f} in {regime}: {'PASS' if passes else 'FAIL'}")

if __name__ == "__main__":
    asyncio.run(test())
