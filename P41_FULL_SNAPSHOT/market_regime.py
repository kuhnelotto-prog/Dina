"""
market_regime.py — Определение рыночного режима.

Режимы:
  - BULL: цена > EMA50 на 4H
  - BEAR: цена < EMA50 на 4H
  - SIDEWAYS: цена ≈ EMA50 (разница < threshold)
  - VOLATILE: ATR > 1.5× среднего ATR
  - CRISIS: BTC в BEAR + ATR > 2× среднего ATR

Используется в:
  - strategist_client.py (пороги входа)
  - risk_manager.py (блокировка/снижение размера)
"""

import logging
from enum import Enum
from typing import Optional, Dict, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"
    VOLATILE = "VOLATILE"
    CRISIS = "CRISIS"


class MarketRegimeDetector:
    """
    Определяет рыночный режим на основе EMA и ATR.
    Работает с кэшем свечей (4H).
    """

    def __init__(
        self,
        ema_period: int = 50,
        atr_period: int = 14,
        sideways_threshold_pct: float = 0.5,
        volatile_atr_mult: float = 1.5,
        crisis_atr_mult: float = 2.0,
        atr_avg_window: int = 50,
    ):
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.sideways_threshold_pct = sideways_threshold_pct
        self.volatile_atr_mult = volatile_atr_mult
        self.crisis_atr_mult = crisis_atr_mult
        self.atr_avg_window = atr_avg_window

        # Кэш свечей: symbol -> DataFrame (4H candles)
        self._candle_cache: Dict[str, pd.DataFrame] = {}

        # Кэш режимов: symbol -> (regime, timestamp)
        self._regime_cache: Dict[str, Tuple[MarketRegime, float]] = {}
        self._cache_ttl: float = 900.0  # 15 минут

    def update_candles(self, symbol: str, df: pd.DataFrame):
        """Обновляет кэш свечей для символа."""
        self._candle_cache[symbol] = df

    def detect(self, symbol: str) -> MarketRegime:
        """
        Определяет текущий режим для символа.
        
        Returns:
            MarketRegime
        """
        import time
        cached = self._regime_cache.get(symbol)
        if cached:
            regime, ts = cached
            if time.time() - ts < self._cache_ttl:
                return regime

        df = self._candle_cache.get(symbol)
        if df is None or len(df) < self.ema_period + self.atr_avg_window:
            return MarketRegime.SIDEWAYS  # недостаточно данных

        # OHLCV column order: [0]ts [1]open [2]high [3]low [4]close [5]volume
        close = df["close"].astype(float) if "close" in df.columns else df.iloc[:, 4].astype(float)
        high = df["high"].astype(float) if "high" in df.columns else df.iloc[:, 2].astype(float)
        low = df["low"].astype(float) if "low" in df.columns else df.iloc[:, 3].astype(float)

        # EMA50
        ema = close.ewm(span=self.ema_period, adjust=False).mean()
        current_price = float(close.iloc[-1])
        current_ema = float(ema.iloc[-1])

        # ATR
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()
        current_atr = float(atr.iloc[-1])
        avg_atr = float(atr.tail(self.atr_avg_window).mean())

        # Определяем базовый режим (BULL/BEAR/SIDEWAYS)
        if current_ema > 0:
            diff_pct = (current_price - current_ema) / current_ema * 100
        else:
            diff_pct = 0

        if abs(diff_pct) < self.sideways_threshold_pct:
            base_regime = MarketRegime.SIDEWAYS
        elif diff_pct > 0:
            base_regime = MarketRegime.BULL
        else:
            base_regime = MarketRegime.BEAR

        # Проверяем волатильность
        atr_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0

        regime = base_regime

        if atr_ratio >= self.crisis_atr_mult and base_regime == MarketRegime.BEAR:
            regime = MarketRegime.CRISIS
        elif atr_ratio >= self.volatile_atr_mult:
            regime = MarketRegime.VOLATILE

        # Кэшируем
        self._regime_cache[symbol] = (regime, time.time())

        logger.debug(
            f"MarketRegime {symbol}: {regime.value} | "
            f"price={current_price:.2f} ema50={current_ema:.2f} diff={diff_pct:+.2f}% | "
            f"atr={current_atr:.2f} avg_atr={avg_atr:.2f} ratio={atr_ratio:.2f}"
        )

        return regime

    def detect_btc_regime(self) -> str:
        """
        Определяет режим BTC (для использования в strategist_client).
        Returns: "BULL" или "BEAR"
        """
        regime = self.detect("BTCUSDT")
        if regime in (MarketRegime.BULL, MarketRegime.SIDEWAYS):
            return "BULL"
        return "BEAR"

    def get_atr_ratio(self, symbol: str) -> float:
        """Возвращает текущий ATR / средний ATR для символа."""
        df = self._candle_cache.get(symbol)
        if df is None or len(df) < self.atr_period + self.atr_avg_window:
            return 1.0

        # OHLCV column order: [0]ts [1]open [2]high [3]low [4]close [5]volume
        close = df["close"].astype(float) if "close" in df.columns else df.iloc[:, 4].astype(float)
        high = df["high"].astype(float) if "high" in df.columns else df.iloc[:, 2].astype(float)
        low = df["low"].astype(float) if "low" in df.columns else df.iloc[:, 3].astype(float)

        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()
        current_atr = float(atr.iloc[-1])
        avg_atr = float(atr.tail(self.atr_avg_window).mean())

        return current_atr / avg_atr if avg_atr > 0 else 1.0

    def is_crisis(self, symbol: str = "BTCUSDT") -> bool:
        """Проверяет, находится ли рынок в режиме CRISIS."""
        return self.detect(symbol) == MarketRegime.CRISIS

    def is_volatile(self, symbol: str = "BTCUSDT") -> bool:
        """Проверяет, находится ли рынок в режиме VOLATILE или CRISIS."""
        regime = self.detect(symbol)
        return regime in (MarketRegime.VOLATILE, MarketRegime.CRISIS)
