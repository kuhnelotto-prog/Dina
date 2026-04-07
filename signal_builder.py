"""
signal_builder.py

Построение торговых сигналов:
  - Технические индикаторы (EMA cross, RSI, MACD, Bollinger, ATR)
  - Мульти‑таймфреймная агрегация (15m/1h/4h)
  - Дополнительные фильтры: FVG, Liquidity Sweep
  - Взвешенный composite_score
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np

from indicators_calc import IndicatorsCalculator
from event_bus import EventBus, BotEvent, EventType
import event_logger

logger = logging.getLogger(__name__)


class SignalBuilder:
    def __init__(self, symbols: List[str], timeframes: Optional[List[str]] = None,
                 learning=None, direction: str = "LONG", bus: Optional[EventBus] = None,
                 shared_signal_time: Optional[Dict[str, float]] = None):
        self._symbols = symbols
        self._timeframes = timeframes or ["15m", "1h", "4h"]
        self._learning = learning
        self._direction = direction
        self._bus = bus

        # Веса таймфреймов (по умолчанию)
        self.timeframe_weights = {
            "15m": 0.2,
            "1h":  0.3,
            "4h":  0.5,
        }

        # Кэш свечей по (symbol, timeframe)
        self._candle_cache: Dict[Tuple[str, str], pd.DataFrame] = {}

        # Базовые веса сигналов (будут обновляться LearningEngine)
        self._weights = self._get_default_weights()

        # Кэш последних сигналов
        self._last_signals: Dict[str, dict] = {}

        # Cooldown между сигналами
        self._signal_cooldown_sec: int = 300  # 5 минут
        self._last_signal_time: Dict[str, float] = shared_signal_time if shared_signal_time is not None else {}

    def _get_default_weights(self) -> dict:
        return {
            "ema_cross": 1.0,
            "volume_spike": 1.0,
            "engulfing": 0.8,
            "fvg": 0.6,
            "macd_cross": 0.5,
            "rsi_filter": 0.4,
            "bb_squeeze": 0.3,
            "whale_confirm": 0.7,
            "sweep": 0.7,
        }

    async def update_candle(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Обновляет кэш свечей для символа и таймфрейма."""
        self._candle_cache[(symbol, timeframe)] = df

    async def compute(self, symbol: str, current_tf: str = "1h") -> dict:
        """
        Вычисляет сигналы для символа.
        Возвращает словарь с индикаторами и composite_score.
        """
        # Cooldown проверка
        import time
        now = time.monotonic()
        last = self._last_signal_time.get(symbol, 0)
        if now - last < self._signal_cooldown_sec:
            return {"symbol": symbol, "error": "cooldown", "composite_score": 0.0}

        # Получаем данные для основного ТФ
        df = self._candle_cache.get((symbol, current_tf))
        if df is None or len(df) < 30:
            logger.warning(f"Not enough data for {symbol} on {current_tf}")
            return {"symbol": symbol, "error": "insufficient_data"}

        calc = IndicatorsCalculator()
        indicators = calc.compute(df)
        if "error" in indicators:
            return {"symbol": symbol, "error": indicators["error"]}

        # Базовый сигнал (основной ТФ)
        signal = {
            "symbol": symbol,
            "price": indicators["price"],
            "rsi": indicators["rsi"],
            "atr": indicators["atr"],
            "atr_pct": indicators["atr_pct"],
            "ema_fast": indicators["ema_fast"],
            "ema_slow": indicators["ema_slow"],
            "volume": indicators["volume"],
            "volume_ratio": indicators["volume_ratio"],
            "macd": indicators["macd"],
            "macd_signal": indicators["macd_signal"],
            "bb_upper": indicators["bb_upper"],
            "bb_lower": indicators["bb_lower"],
            "bb_middle": indicators["bb_middle"],
            "engulfing_bull": indicators["engulfing_bull"],
            "engulfing_bear": indicators["engulfing_bear"],
            "fvg_bull": indicators["fvg_bull"],
            "fvg_bear": indicators["fvg_bear"],
            # Добавим sweep, если есть
            "sweep_bull": indicators.get("sweep_bull", False),
            "sweep_bear": indicators.get("sweep_bear", False),
            "composite_score": 0.0,
        }

        # Мульти‑таймфреймная агрегация
        multi_score, tf_signals = await self._aggregate_timeframes(symbol, self._direction)
        signal["composite_score"] = multi_score
        signal["tf_signals"] = tf_signals

        # Если LearningEngine есть, применяем веса
        if self._learning:
            weights = await self._learning.get_weights()
            signal["weights"] = weights

        self._last_signals[symbol] = signal
        self._last_signal_time[symbol] = time.monotonic()
        # Логируем сигнал
        event_logger.signal_generated(
            symbol=symbol,
            direction=self._direction,
            composite_score=multi_score,
            rsi=indicators["rsi"],
            volume_ratio=indicators["volume_ratio"]
        )
        return signal

    async def _aggregate_timeframes(self, symbol: str, direction: str) -> Tuple[float, dict]:
        """
        Собирает сигналы со всех таймфреймов и возвращает взвешенный composite_score.
        """
        total_score = 0.0
        total_weight = 0.0
        signals_by_tf = {}
        missing_tfs = []

        for tf, weight in self.timeframe_weights.items():
            df = self._candle_cache.get((symbol, tf))
            if df is None or len(df) < 30:
                missing_tfs.append(tf)
                continue

            calc = IndicatorsCalculator()
            indicators = calc.compute(df)
            if "error" in indicators:
                missing_tfs.append(tf)
                continue

            # Вычисляем сигнал для этого таймфрейма
            tf_signal = self._calculate_signal_from_indicators(indicators, direction)
            composite = self._calculate_composite(tf_signal, self._weights)
            total_score += composite * weight
            total_weight += weight
            signals_by_tf[tf] = composite

        # Защита: если не хватает данных хотя бы по одному ТФ — не торгуем
        if missing_tfs:
            logger.debug(f"Missing timeframes for {symbol}: {missing_tfs}")
            return 0.0, {}

        if total_weight == 0:
            return 0.0, {}

        return total_score / total_weight, signals_by_tf

    def _calculate_signal_from_indicators(self, indicators: dict, direction: str) -> dict:
        """
        Преобразует набор индикаторов в словарь булевых сигналов.
        """
        # Определяем направление для взвешивания
        # Для LONG-бота бычьи сигналы дают +1, медвежьи -1
        # Для SHORT-бота наоборот
        mult = 1 if direction == "LONG" else -1

        # EMA cross
        ema_cross_bull = (indicators["ema_fast"] > indicators["ema_slow"]) and (
            indicators.get("ema_fast_prev", 0) <= indicators.get("ema_slow_prev", 0)
        )
        ema_cross_bear = (indicators["ema_fast"] < indicators["ema_slow"]) and (
            indicators.get("ema_fast_prev", 0) >= indicators.get("ema_slow_prev", 0)
        )

        # Volume spike
        volume_spike = indicators.get("volume_ratio", 1.0) > 1.2

        # Engulfing
        engulfing_bull = indicators.get("engulfing_bull", False)
        engulfing_bear = indicators.get("engulfing_bear", False)

        # FVG
        fvg_bull = indicators.get("fvg_bull", False)
        fvg_bear = indicators.get("fvg_bear", False)

        # MACD cross
        macd_cross = False
        if "macd" in indicators and "macd_signal" in indicators:
            # Временно отключаем MACD cross для избежания ложных сигналов
            macd_cross = False

        # Bollinger squeeze
        bb_squeeze = (indicators["bb_upper"] - indicators["bb_lower"]) / indicators["bb_middle"] < 0.05 if indicators["bb_middle"] > 0 else False

        # RSI filter (значение)
        rsi = indicators.get("rsi", 50)

        # Sweep
        sweep_bull = indicators.get("sweep_bull", False)
        sweep_bear = indicators.get("sweep_bear", False)

        # Результирующие сигналы (с учётом направления)
        signals = {
            "ema_cross_bull": ema_cross_bull,
            "ema_cross_bear": ema_cross_bear,
            "volume_spike": volume_spike,
            "engulfing_bull": engulfing_bull,
            "engulfing_bear": engulfing_bear,
            "fvg_bull": fvg_bull,
            "fvg_bear": fvg_bear,
            "macd_cross": macd_cross,
            "bb_squeeze": bb_squeeze,
            "rsi": rsi,
            "sweep_bull": sweep_bull,
            "sweep_bear": sweep_bear,
        }
        return signals

    def _calculate_composite(self, signals: dict, weights: dict) -> float:
        """
        Считает взвешенный композитный сигнал от -1 до 1.
        Нормализация: сумма набранных весов / максимально возможная сумма всех весов.
        """
        score = 0.0

        # Максимально возможная сумма всех весов (для нормализации)
        max_possible = (
            weights.get("ema_cross", 1.0) +
            weights.get("volume_spike", 1.0) +
            weights.get("engulfing", 0.8) +
            weights.get("fvg", 0.6) +
            weights.get("macd_cross", 0.5) +
            weights.get("bb_squeeze", 0.3) +
            weights.get("sweep", 0.7)
        )

        # Направление: для LONG-бота +1 для бычьих, -1 для медвежьих; для SHORT наоборот
        direction_mult = 1 if self._direction == "LONG" else -1

        if signals["ema_cross_bull"]:
            score += weights.get("ema_cross", 1.0) * direction_mult
        elif signals["ema_cross_bear"]:
            score += weights.get("ema_cross", 1.0) * -direction_mult

        if signals["engulfing_bull"]:
            score += weights.get("engulfing", 0.8) * direction_mult
        elif signals["engulfing_bear"]:
            score += weights.get("engulfing", 0.8) * -direction_mult

        if signals["fvg_bull"]:
            score += weights.get("fvg", 0.6) * direction_mult
        elif signals["fvg_bear"]:
            score += weights.get("fvg", 0.6) * -direction_mult

        if signals["volume_spike"]:
            score += weights.get("volume_spike", 1.0) * direction_mult

        if signals["macd_cross"]:
            score += weights.get("macd_cross", 0.5) * direction_mult

        if signals["bb_squeeze"]:
            score += weights.get("bb_squeeze", 0.3)

        if signals["sweep_bull"]:
            score += weights.get("sweep", 0.7) * direction_mult
        elif signals["sweep_bear"]:
            score += weights.get("sweep", 0.7) * -direction_mult

        if max_possible > 0:
            return score / max_possible
        return 0.0

    def get_signal_summary(self, symbol: str) -> dict:
        if symbol not in self._last_signals:
            return {"symbol": symbol, "composite_score": 0.0}
        s = self._last_signals[symbol]
        return {
            "symbol": symbol,
            "composite_score": s.get("composite_score", 0.0),
            "direction": "bullish" if s.get("composite_score", 0) > 0.2 else
                        "bearish" if s.get("composite_score", 0) < -0.2 else "neutral",
            "rsi": s.get("rsi", 50),
            "volume_ratio": s.get("volume_ratio", 1.0)
        }