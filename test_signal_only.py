#!/usr/bin/env python3
"""
Тест только генерации сигналов (без фильтров)
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
    # Создаём простые данные для одного таймфрейма
    dates = pd.date_range(start='2026-01-01', periods=200, freq='4h')
    trend = np.linspace(100, 150, 200)
    noise = np.random.randn(200) * 2
    df_4h = pd.DataFrame({
        'open': trend + noise,
        'high': trend + noise + 2,
        'low': trend + noise - 2,
        'close': trend + noise,
        'volume': np.random.rand(200) * 1000 + 500
    }, index=dates)
    
    # Создаём SignalBuilder только с 4h
    sb = SignalBuilder(symbols=['TEST'], timeframes=['4h'], direction='LONG')
    sb._signal_cooldown_sec = 0
    sb.timeframe_weights = {'4h': 1.0}  # только один ТФ
    
    await sb.update_candle('TEST', '4h', df_4h)
    
    # Проверяем сигнал
    signal = await sb.compute('TEST', '4h')
    
    print("Signal details:")
    print(f"  Composite score: {signal.get('composite_score', 0):.3f}")
    print(f"  Filtered: {signal.get('filtered', False)}")
    print(f"  Regime: {sb.detect_regime('TEST')}")
    
    # Проверяем внутреннюю логику
    print("\nInternal check:")
    # Получаем индикаторы напрямую
    from indicators_calc import IndicatorsCalculator
    calc = IndicatorsCalculator()
    indicators = calc.compute(df_4h)
    
    print(f"  EMA fast: {indicators['ema_fast']:.2f}, slow: {indicators['ema_slow']:.2f}")
    print(f"  RSI: {indicators['rsi']:.1f}")
    print(f"  MACD: {indicators['macd']:.3f}, signal: {indicators['macd_signal']:.3f}")
    print(f"  Price: {indicators['price']:.2f}, BB lower: {indicators['bb_lower']:.2f}")
    
    # Проверяем STATE компоненты
    print("\nSTATE components:")
    state = 0
    if indicators['ema_fast'] > indicators['ema_slow']:
        print("  ✓ EMA bullish")
        state += 1
    if indicators['rsi'] < 40:
        print(f"  ✓ RSI low ({indicators['rsi']:.1f})")
        state += 1
    if indicators['macd'] > indicators['macd_signal']:
        print("  ✓ MACD bullish")
        state += 1
    if indicators['price'] < indicators['bb_lower']:
        print("  ✓ BB below lower")
        state += 1
    
    print(f"\nTotal STATE components: {state}/4")
    
    # Проверяем фильтры вручную
    regime = sb.detect_regime('TEST')
    print(f"\nFilter check for regime {regime}:")
    
    # Порог
    if regime == "BEAR":
        threshold = 0.40
    elif regime == "SIDEWAYS":
        threshold = 0.45
    else:
        threshold = 0.30
    
    score = signal.get('composite_score', 0)
    print(f"  Threshold: {threshold}, Score: {score:.3f}")
    print(f"  Passes threshold: {'YES' if score >= threshold else 'NO'}")
    
    # ATR
    atr_pct = indicators['atr_pct']
    print(f"  ATR: {atr_pct:.2f}% (min 0.3%)")
    print(f"  Passes ATR: {'YES' if atr_pct >= 0.3 else 'NO'}")

if __name__ == "__main__":
    asyncio.run(test())
