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
        # 1d не участвует в composite — используется только как тренд-фильтр
        self.timeframe_weights = {
            "15m": 0.2,
            "1h":  0.3,
            "4h":  0.5,
        }

        # EMA50 на 1D — глобальный тренд-фильтр
        self._ema50_1d_window = 50

        # Режимный фильтр: EMA20 vs EMA50 на 4H
        self._regime_ema_fast = 20
        self._regime_ema_slow = 50
        self._regime_threshold_pct = 0.5  # разница < 0.5% = SIDEWAYS

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

        # Применяем фильтры на основе анализа winning/losing сделок
        regime = self.detect_regime(symbol)
        passes_filters = self._apply_filters(signal, multi_score, regime)
        
        if not passes_filters:
            logger.info(f"🚫 {symbol} {self._direction} filtered out: "
                       f"composite={multi_score:.3f}, regime={regime}")
            signal["composite_score"] = 0.0  # обнуляем сигнал
            signal["filtered"] = True
        else:
            signal["filtered"] = False

        self._last_signals[symbol] = signal
        self._last_signal_time[symbol] = time.monotonic()
        
        # Логируем сигнал
        event_logger.signal_generated(
            symbol=symbol,
            direction=self._direction,
            composite_score=multi_score,
            rsi=indicators["rsi"],
            volume_ratio=indicators["volume_ratio"],
            regime=regime,
            filtered=not passes_filters
        )
        return signal

    async def _aggregate_timeframes(self, symbol: str, direction: str) -> Tuple[float, dict]:
        """
        Собирает сигналы со всех таймфреймов и возвращает взвешенный composite_score.
        Перед расчётом проверяет глобальный тренд-фильтр EMA50 на 1D.
        """
        # ── Глобальный тренд-фильтр: EMA50 на 1D ──
        if not self._check_daily_trend(symbol, direction):
            return 0.0, {}

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

    def _check_daily_trend(self, symbol: str, direction: str) -> bool:
        """
        Глобальный тренд-фильтр: EMA50 на 1D таймфрейме.
        
        LONG: разрешён только если close_1D > EMA50_1D (бычий тренд)
        SHORT: разрешён только если close_1D < EMA50_1D (медвежий тренд)
        
        Если данных 1D нет — пропускаем фильтр (разрешаем вход).
        """
        df_1d = self._candle_cache.get((symbol, "1d"))
        if df_1d is None or len(df_1d) < self._ema50_1d_window:
            # Нет данных 1D — не блокируем (graceful degradation)
            logger.debug(f"No 1D data for {symbol}, skipping daily trend filter")
            return True

        close = df_1d["close"] if "close" in df_1d.columns else df_1d.iloc[:, 3]
        ema50 = close.ewm(span=self._ema50_1d_window, adjust=False).mean()
        
        current_close = float(close.iloc[-1])
        current_ema50 = float(ema50.iloc[-1])

        if direction == "LONG" and current_close < current_ema50:
            logger.info(
                f"🚫 {symbol} LONG blocked by 1D trend filter: "
                f"close={current_close:.2f} < EMA50={current_ema50:.2f}"
            )
            return False

        if direction == "SHORT" and current_close > current_ema50:
            logger.info(
                f"🚫 {symbol} SHORT blocked by 1D trend filter: "
                f"close={current_close:.2f} > EMA50={current_ema50:.2f}"
            )
            return False

        return True

    def _calculate_signal_from_indicators(self, indicators: dict, direction: str) -> dict:
        """
        Преобразует набор индикаторов в словарь сигналов.
        
        Архитектура v2: STATE-based + EVENT-based сигналы.
        - STATE сигналы (ema_trend, rsi_zone, macd_histogram, bb_position)
          дают базовый фон — активны на каждой свече.
        - EVENT сигналы (ema_cross, engulfing, fvg, sweep)
          дают бонус при совпадении — редкие, но сильные.
        """
        # ── STATE сигналы (активны постоянно) ──
        
        # EMA trend state: fast vs slow
        ema_bullish = indicators["ema_fast"] > indicators["ema_slow"]
        ema_bearish = indicators["ema_fast"] < indicators["ema_slow"]
        
        # EMA cross (event — бонус)
        ema_cross_bull = (indicators["ema_fast"] > indicators["ema_slow"]) and (
            indicators.get("ema_fast_prev", 0) <= indicators.get("ema_slow_prev", 0)
        )
        ema_cross_bear = (indicators["ema_fast"] < indicators["ema_slow"]) and (
            indicators.get("ema_fast_prev", 0) >= indicators.get("ema_slow_prev", 0)
        )

        # RSI zone (state)
        rsi = indicators.get("rsi", 50)
        # Для SHORT: RSI > 70 = перекупленность (хорошо для шорта)
        # Для LONG: RSI < 30 = перепроданность (хорошо для лонга)
        rsi_overbought = rsi > 70
        rsi_oversold = rsi < 30
        # Мягкие зоны
        rsi_high = rsi > 60  # умеренно перекуплен
        rsi_low = rsi < 40   # умеренно перепродан

        # MACD histogram state
        macd = indicators.get("macd", 0)
        macd_signal = indicators.get("macd_signal", 0)
        macd_hist = macd - macd_signal
        macd_bullish = macd_hist > 0
        macd_bearish = macd_hist < 0

        # Bollinger position (state)
        price = indicators.get("price", 0)
        bb_upper = indicators.get("bb_upper", 0)
        bb_lower = indicators.get("bb_lower", 0)
        bb_middle = indicators.get("bb_middle", 0)
        # Цена выше верхней BB = перекупленность
        bb_above_upper = price > bb_upper if bb_upper > 0 else False
        # Цена ниже нижней BB = перепроданность
        bb_below_lower = price < bb_lower if bb_lower > 0 else False
        # BB squeeze (state)
        bb_squeeze = (bb_upper - bb_lower) / bb_middle < 0.05 if bb_middle > 0 else False

        # Volume spike
        volume_spike = indicators.get("volume_ratio", 1.0) > 1.2

        # ── EVENT сигналы (бонусы) ──
        engulfing_bull = indicators.get("engulfing_bull", False)
        engulfing_bear = indicators.get("engulfing_bear", False)
        fvg_bull = indicators.get("fvg_bull", False)
        fvg_bear = indicators.get("fvg_bear", False)
        sweep_bull = indicators.get("sweep_bull", False)
        sweep_bear = indicators.get("sweep_bear", False)

        signals = {
            # States
            "ema_bullish": ema_bullish,
            "ema_bearish": ema_bearish,
            "rsi": rsi,
            "rsi_overbought": rsi_overbought,
            "rsi_oversold": rsi_oversold,
            "rsi_high": rsi_high,
            "rsi_low": rsi_low,
            "macd_bullish": macd_bullish,
            "macd_bearish": macd_bearish,
            "bb_above_upper": bb_above_upper,
            "bb_below_lower": bb_below_lower,
            "bb_squeeze": bb_squeeze,
            "volume_spike": volume_spike,
            # Events (бонусы)
            "ema_cross_bull": ema_cross_bull,
            "ema_cross_bear": ema_cross_bear,
            "engulfing_bull": engulfing_bull,
            "engulfing_bear": engulfing_bear,
            "fvg_bull": fvg_bull,
            "fvg_bear": fvg_bear,
            "sweep_bull": sweep_bull,
            "sweep_bear": sweep_bear,
        }
        return signals

    def _calculate_composite(self, signals: dict, weights: dict) -> float:
        """
        Считает взвешенный композитный сигнал от -1 до 1.
        
        Архитектура v2:
        - STATE слой (60% веса): ema_trend + rsi_zone + macd_hist + bb_position
          Активны на КАЖДОЙ свече, дают базовый фон.
        - EVENT слой (40% веса): ema_cross + engulfing + fvg + sweep
          Редкие бонусы, усиливают сигнал при совпадении.
        - Volume spike = множитель x1.2 (не в score).
        """
        # Направление: LONG=+1, SHORT=-1
        d = 1 if self._direction == "LONG" else -1

        # ── STATE слой (базовый фон, max = 1.0) ──
        state_score = 0.0
        state_max = 4.0  # 4 компонента по 1.0

        # 1. EMA trend state (вес 1.0)
        if signals.get("ema_bullish"):
            state_score += 1.0 * d
        elif signals.get("ema_bearish"):
            state_score += 1.0 * -d

        # 2. RSI zone (вес 1.0)
        rsi = signals.get("rsi", 50)
        if signals.get("rsi_overbought"):       # RSI > 70
            state_score += 1.0 * -d             # медвежий сигнал
        elif signals.get("rsi_oversold"):        # RSI < 30
            state_score += 1.0 * d              # бычий сигнал
        elif signals.get("rsi_high"):            # RSI > 60
            state_score += 0.4 * -d             # слабо медвежий
        elif signals.get("rsi_low"):             # RSI < 40
            state_score += 0.4 * d              # слабо бычий

        # 3. MACD histogram (вес 1.0)
        if signals.get("macd_bearish"):
            state_score += 1.0 * -d
        elif signals.get("macd_bullish"):
            state_score += 1.0 * d

        # 4. Bollinger position (вес 1.0)
        if signals.get("bb_above_upper"):
            state_score += 1.0 * -d             # перекупленность → медвежий
        elif signals.get("bb_below_lower"):
            state_score += 1.0 * d              # перепроданность → бычий
        elif signals.get("bb_squeeze"):
            state_score += 0.3                  # нейтральный бонус (готовность к движению)

        # Нормализуем state: [-1, +1]
        state_normalized = state_score / state_max if state_max > 0 else 0.0

        # ── EVENT слой (бонусы) ──
        event_score = 0.0
        # Используем веса из словаря weights
        ema_cross_weight = weights.get("ema_cross", 1.0)
        engulfing_weight = weights.get("engulfing", 0.8)
        fvg_weight = weights.get("fvg", 0.6)
        sweep_weight = weights.get("sweep", 0.7)
        
        event_max = ema_cross_weight + engulfing_weight + fvg_weight + sweep_weight

        # EMA cross (event)
        if signals.get("ema_cross_bull"):
            event_score += ema_cross_weight * d
        elif signals.get("ema_cross_bear"):
            event_score += ema_cross_weight * -d

        # Engulfing
        if signals.get("engulfing_bull"):
            event_score += engulfing_weight * d
        elif signals.get("engulfing_bear"):
            event_score += engulfing_weight * -d

        # FVG
        if signals.get("fvg_bull"):
            event_score += fvg_weight * d
        elif signals.get("fvg_bear"):
            event_score += fvg_weight * -d

        # Sweep
        if signals.get("sweep_bull"):
            event_score += sweep_weight * d
        elif signals.get("sweep_bear"):
            event_score += sweep_weight * -d

        # Нормализуем event: [-1, +1]
        event_normalized = event_score / event_max if event_max > 0 else 0.0

        # ── Итоговый composite: 60% state + 40% event ──
        composite = 0.60 * state_normalized + 0.40 * event_normalized

        # Volume spike как множитель
        volume_spike_multiplier = weights.get("volume_spike", 1.2)
        if signals.get("volume_spike", False):
            composite *= volume_spike_multiplier

        # Clamp to [-1, +1]
        return max(-1.0, min(1.0, composite))

    def _apply_filters(self, signals: dict, composite_score: float, regime: str) -> bool:
        """
        Применяет фильтры для входа на основе анализа winning/losing сделок.
        
        Returns: True если сигнал проходит фильтры, False если нужно пропустить.
        """
        # 1. Режим-зависимый порог
        if regime == "BEAR":
            threshold = 0.45
        elif regime == "SIDEWAYS":
            threshold = 0.50
        else:  # BULL
            threshold = 0.35
        
        # Проверяем порог (учитываем направление)
        if self._direction == "LONG":
            if composite_score < threshold:
                return False
        else:  # SHORT
            if composite_score > -threshold:
                return False
        
        # 2. ATR фильтр (пропускать при малой волатильности)
        atr_pct = signals.get("atr_pct", 0)
        if atr_pct < 0.5:  # ATR < 0.5%
            return False
        
        # 3. STATE компоненты: минимум 2 из 4 в одном направлении
        state_components = 0
        d = 1 if self._direction == "LONG" else -1
        
        # EMA trend
        if (self._direction == "LONG" and signals.get("ema_bullish")) or \
           (self._direction == "SHORT" and signals.get("ema_bearish")):
            state_components += 1
        
        # RSI zone
        rsi = signals.get("rsi", 50)
        if self._direction == "LONG" and (signals.get("rsi_oversold") or signals.get("rsi_low")):
            state_components += 1
        elif self._direction == "SHORT" and (signals.get("rsi_overbought") or signals.get("rsi_high")):
            state_components += 1
        
        # MACD
        if (self._direction == "LONG" and signals.get("macd_bullish")) or \
           (self._direction == "SHORT" and signals.get("macd_bearish")):
            state_components += 1
        
        # Bollinger position
        if (self._direction == "LONG" and signals.get("bb_below_lower")) or \
           (self._direction == "SHORT" and signals.get("bb_above_upper")):
            state_components += 1
        
        if state_components < 2:
            return False
        
        # Все фильтры пройдены
        return True

    def detect_regime(self, symbol: str) -> str:
        """
        Определяет рыночный режим по EMA20 vs EMA50 на 4H.
        
        Returns: "BULL", "BEAR", или "SIDEWAYS"
        """
        df_4h = self._candle_cache.get((symbol, "4h"))
        if df_4h is None or len(df_4h) < self._regime_ema_slow:
            return "SIDEWAYS"  # недостаточно данных — нейтральный режим

        close = df_4h["close"] if "close" in df_4h.columns else df_4h.iloc[:, 3]
        ema_fast = close.ewm(span=self._regime_ema_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self._regime_ema_slow, adjust=False).mean()

        current_fast = float(ema_fast.iloc[-1])
        current_slow = float(ema_slow.iloc[-1])

        if current_slow == 0:
            return "SIDEWAYS"

        diff_pct = (current_fast - current_slow) / current_slow * 100

        if diff_pct > self._regime_threshold_pct:
            return "BULL"
        elif diff_pct < -self._regime_threshold_pct:
            return "BEAR"
        else:
            return "SIDEWAYS"

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