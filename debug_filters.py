#!/usr/bin/env python3
"""
Отладка фильтров
"""
import asyncio
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

import sys
sys.path.insert(0, '.')

from signal_builder import SignalBuilder

async def test():
    # Создаём тестовые данные с явными сигналами
    dates = pd.date_range(start='2026-01-01', periods=100, freq='4h')
    # Создаём тренд вверх
    trend = np.linspace(100, 150, 100)
    noise = np.random.randn(100) * 2
    df = pd.DataFrame({
        'open': trend + noise,
        'high': trend + noise + 2,
        'low': trend + noise - 2,
        'close': trend + noise,
        'volume': np.random.rand(100) * 1000 + 500
    }, index=dates)
    
    # Создаём SignalBuilder
    sb = SignalBuilder(symbols=['TEST'], direction='LONG')
    sb._signal_cooldown_sec = 0
    
    await sb.update_candle('TEST', '4h', df)
    
    # Получаем сигнал
    signal = await sb.compute('TEST', '4h')
    
    print("Signal details:")
    for key, val in signal.items():
        if isinstance(val, (int, float, bool, str)):
            print(f"  {key}: {val}")
    
    # Проверяем STATE компоненты
    print("\nSTATE components check:")
    state_components = 0
    
    # EMA trend
    if signal.get('ema_bullish'):
        print("  ✓ EMA bullish")
        state_components += 1
    else:
        print("  ✗ EMA not bullish")
    
    # RSI zone
    rsi = signal.get('rsi', 50)
    if rsi < 40:
        print(f"  ✓ RSI low ({rsi:.1f})")
        state_components += 1
    else:
        print(f"  ✗ RSI not low ({rsi:.1f})")
    
    # MACD
    if signal.get('macd_bullish'):
        print("  ✓ MACD bullish")
        state_components += 1
    else:
        print("  ✗ MACD not bullish")
    
    # Bollinger position
    if signal.get('bb_below_lower'):
        print("  ✓ BB below lower")
        state_components += 1
    else:
        print("  ✗ BB not below lower")
    
    print(f"\nTotal STATE components: {state_components}/4")
    
    # Проверяем фильтры вручную
    regime = sb.detect_regime('TEST')
    print(f"\nRegime: {regime}")
    print(f"Composite score: {signal.get('composite_score', 0):.3f}")
    
    # Проверяем порог
    if regime == "BEAR":
        threshold = 0.40
    elif regime == "SIDEWAYS":
        threshold = 0.45
    else:
        threshold = 0.30
    
    score = signal.get('composite_score', 0)
    if score < threshold:
        print(f"✗ Threshold fail: {score:.3f} < {threshold}")
    else:
        print(f"✓ Threshold pass: {score:.3f} >= {threshold}")
    
    # Проверяем ATR
    atr_pct = signal.get('atr_pct', 0)
    if atr_pct < 0.3:
        print(f"✗ ATR fail: {atr_pct:.2f}% < 0.3%")
    else:
        print(f"✓ ATR pass: {atr_pct:.2f}% >= 0.3%")
    
    # Проверяем STATE компоненты
    if state_components < 2:
        print(f"✗ STATE components fail: {state_components} < 2")
    else:
        print(f"✓ STATE components pass: {state_components} >= 2")

if __name__ == "__main__":
    asyncio.run(test())
