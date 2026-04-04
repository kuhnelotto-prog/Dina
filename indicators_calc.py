"""
indicators_calc.py — вычисление технических индикаторов.
Добавлены FVG и Liquidity Sweep.
"""

import pandas as pd
import numpy as np
import ta

from typing import Optional

class IndicatorsCalculator:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self._ema_fast = int(self.config.get("EMA_FAST", 9))
        self._ema_slow = int(self.config.get("EMA_SLOW", 21))

    def compute(self, df: pd.DataFrame) -> dict:
        """Вычисляет все индикаторы для DataFrame"""
        if df is None or len(df) < 30:
            return {
                "error": "insufficient_data",
                "rsi": 50,
                "atr": 0,
                "atr_pct": 0,
                "ema_fast": 0,
                "ema_slow": 0
            }

        df = self._ensure_ascending(df)

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # Индикаторы
        rsi = ta.momentum.rsi(close, window=14)
        ema_fast = ta.trend.ema_indicator(close, window=self._ema_fast)
        ema_slow = ta.trend.ema_indicator(close, window=self._ema_slow)
        atr = ta.volatility.average_true_range(high, low, close, window=14)

        # MACD
        macd = ta.trend.macd(close)
        macd_signal = ta.trend.macd_signal(close)

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)

        # Объём
        vol_sma = volume.rolling(window=20).mean()

        last = len(df) - 1
        prev = len(df) - 2
        pprev = len(df) - 3

        result = {
            "price": float(close.iloc[last]),
            "rsi": float(rsi.iloc[last]) if last > 14 else 50.0,
            "atr": float(atr.iloc[last]) if not pd.isna(atr.iloc[last]) else 0.0,
            "atr_pct": float(atr.iloc[last] / close.iloc[last] * 100) if atr.iloc[last] > 0 else 0.0,
            "ema_fast": float(ema_fast.iloc[last]) if not pd.isna(ema_fast.iloc[last]) else 0.0,
            "ema_slow": float(ema_slow.iloc[last]) if not pd.isna(ema_slow.iloc[last]) else 0.0,
            "ema_fast_prev": float(ema_fast.iloc[prev]) if len(df) > 1 else 0.0,
            "ema_slow_prev": float(ema_slow.iloc[prev]) if len(df) > 1 else 0.0,
            "volume": float(volume.iloc[last]),
            "volume_ratio": float(volume.iloc[last] / vol_sma.iloc[last]) if vol_sma.iloc[last] > 0 else 1.0,
            "macd": float(macd.iloc[last]) if not pd.isna(macd.iloc[last]) else 0.0,
            "macd_signal": float(macd_signal.iloc[last]) if not pd.isna(macd_signal.iloc[last]) else 0.0,
            "bb_upper": float(bb.bollinger_hband().iloc[last]) if not pd.isna(bb.bollinger_hband().iloc[last]) else 0.0,
            "bb_lower": float(bb.bollinger_lband().iloc[last]) if not pd.isna(bb.bollinger_lband().iloc[last]) else 0.0,
            "bb_middle": float(bb.bollinger_mavg().iloc[last]) if not pd.isna(bb.bollinger_mavg().iloc[last]) else 0.0,
        }

        # Свечные паттерны + FVG + Sweep
        result.update(self._check_patterns(df))
        result.update(self._check_sweep(df))

        return result

    def _check_patterns(self, df: pd.DataFrame) -> dict:
        """Проверяет свечные паттерны и FVG."""
        last = len(df) - 1
        prev = len(df) - 2
        pprev = len(df) - 3

        close = df["close"]
        open_ = df["open"]
        high = df["high"]
        low = df["low"]

        # Бычий поглощающий
        bullish_engulfing = (
            close.iloc[last] > open_.iloc[last] and
            close.iloc[prev] < open_.iloc[prev] and
            close.iloc[last] > open_.iloc[prev] and
            open_.iloc[last] < close.iloc[prev]
        )

        # Медвежий поглощающий
        bearish_engulfing = (
            close.iloc[last] < open_.iloc[last] and
            close.iloc[prev] > open_.iloc[prev] and
            close.iloc[last] < open_.iloc[prev] and
            open_.iloc[last] > close.iloc[prev]
        )

        # Fair Value Gap (бычий)
        fvg_bull = low.iloc[last] > high.iloc[pprev] if pprev >= 0 else False
        fvg_bear = high.iloc[last] < low.iloc[pprev] if pprev >= 0 else False

        return {
            "engulfing_bull": bullish_engulfing,
            "engulfing_bear": bearish_engulfing,
            "fvg_bull": fvg_bull,
            "fvg_bear": fvg_bear
        }

    def _check_sweep(self, df: pd.DataFrame) -> dict:
        """
        Проверяет Liquidity Sweep: цена пробила предыдущий хай/лой,
        но закрылась внутри диапазона (разворот).
        """
        if len(df) < 3:
            return {"sweep_bull": False, "sweep_bear": False}

        last = len(df) - 1
        prev = len(df) - 2

        high = df["high"]
        low = df["low"]
        close = df["close"]

        # Предыдущий хай и лой (можно брать за последние N свечей, упростим)
        prev_high = high.iloc[prev]
        prev_low = low.iloc[prev]

        # Бычий sweep: цена пробила предыдущий лой, но закрылась выше него
        sweep_bull = (low.iloc[last] < prev_low) and (close.iloc[last] > prev_low)

        # Медвежий sweep: цена пробила предыдущий хай, но закрылась ниже него
        sweep_bear = (high.iloc[last] > prev_high) and (close.iloc[last] < prev_high)

        return {"sweep_bull": sweep_bull, "sweep_bear": sweep_bear}

    @staticmethod
    def _ensure_ascending(df: pd.DataFrame) -> pd.DataFrame:
        if "timestamp" in df.columns:
            if df["timestamp"].iloc[0] > df["timestamp"].iloc[-1]:
                df = df.sort_values("timestamp").reset_index(drop=True)
        return df