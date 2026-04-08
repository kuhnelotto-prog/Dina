"""
risk_manager.py

Финальный страж перед исполнением.
Проверяет:
  - Дневной лимит потерь
  - Лимит открытых позиций
  - Общую экспозицию по всем символам
  - Корреляцию между позициями
  - Просадку и серию потерь (через PositionSizer)
  - Аварийные стопы
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Tuple

import numpy as np
import pandas as pd

from position_sizer import PositionSizer, PortfolioState, SizerConfig, SizerDecision, SizeResult

logger = logging.getLogger(__name__)


class DrawdownState(str, Enum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    EMERGENCY = "EMERGENCY"


@dataclass
class RiskStatus:
    allowed: bool
    state: DrawdownState
    reason: str = ""
    size_result: Optional[SizeResult] = None

    @property
    def risk_status_str(self) -> str:
        return "OK" if self.allowed else "BLOCKED"

    @property
    def drawdown_state_str(self) -> str:
        return self.state.value


class RiskManager:
    def __init__(
        self,
        sizer_config: Optional[SizerConfig] = None,
        max_open_positions: int = 1,
        daily_loss_limit: float = 5.0,
        max_total_exposure_usd: float = 5000.0,
    ):
        self.sizer = PositionSizer(sizer_config or SizerConfig())
        self.max_open_positions = max_open_positions
        self.daily_loss_limit = daily_loss_limit
        self.max_total_exposure_usd = max_total_exposure_usd

        self._open_positions: Dict[str, dict] = {}          # symbol -> position info
        self._open_count: int = 0
        self._daily_pnl: float = 0.0
        self._day_start: float = time.time()

        # Кэш свечей 4H для расчёта корреляции секторов
        # (symbol, "4h") -> pd.DataFrame с колонкой "close"
        self._candle_cache: Dict[str, pd.DataFrame] = {}

        # Секторные группы
        self.SECTOR_GROUPS = {
            "L1": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "DOTUSDT"],
            "DeFi": ["LINKUSDT", "AAVEUSDT", "UNIUSDT"],
            "Meme": ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT"],
            "Alt_L1": ["BNBUSDT", "ADAUSDT", "XRPUSDT"],
        }

        # Кэш корреляций секторов (обновляется раз в цикл)
        self._sector_corr_cache: Dict[str, float] = {}
        self._corr_cache_ts: float = 0.0
        self._corr_cache_ttl: float = 3600.0  # обновлять раз в час

        logger.info(f"RiskManager init | max_pos={max_open_positions} | daily_loss_limit={daily_loss_limit}% | total_exposure_limit=${max_total_exposure_usd}")

    # ============================================================
    # Основная проверка перед входом
    # ============================================================

    async def check(
        self,
        portfolio: PortfolioState,
        symbol: str,
        entry_price: float,
        sl_price: float,
        confidence: float,
        atr_pct: Optional[float] = None,
        win_rate: Optional[float] = None,
        avg_rr: Optional[float] = None,
        direction: str = "long",
    ) -> RiskStatus:
        # Сброс дневного счётчика
        self._reset_daily_if_needed()

        # 1. Лимит открытых позиций
        if self._open_count >= self.max_open_positions:
            return RiskStatus(
                allowed=False,
                state=DrawdownState.NORMAL,
                reason=f"Already {self._open_count} positions (limit {self.max_open_positions})"
            )

        # 2. Дневной лимит потерь
        daily_loss_pct = self._daily_pnl / portfolio.balance * 100 if portfolio.balance else 0
        if daily_loss_pct <= -self.daily_loss_limit:
            return RiskStatus(
                allowed=False,
                state=DrawdownState.EMERGENCY,
                reason=f"Daily loss limit reached: {daily_loss_pct:.1f}% ≤ -{self.daily_loss_limit}%"
            )

        # 3. Общая экспозиция (сумма size_usd всех открытых позиций)
        total_exposure = sum(p.get("size_usd", 0) for p in self._open_positions.values())
        # Оценка размера новой позиции: risk_usd / sl_distance * leverage
        sl_dist_pct = abs(entry_price - sl_price) / entry_price if entry_price > 0 else 0.01
        estimated_position_usd = (portfolio.balance * self.sizer.cfg.base_risk_pct / 100) / max(sl_dist_pct, 0.001)
        if total_exposure + estimated_position_usd > self.max_total_exposure_usd:
            return RiskStatus(
                allowed=False,
                state=DrawdownState.NORMAL,
                reason=f"Total exposure would exceed ${self.max_total_exposure_usd}"
            )

        # 4. Корреляция с существующими позициями (группы L1, DeFi, Meme + BTC)
        if not self._check_correlation(symbol, direction):
            return RiskStatus(
                allowed=False,
                state=DrawdownState.NORMAL,
                reason="Correlation limit exceeded (same sector already open)"
            )

                # 5. Расчёт размера через PositionSizer (учитывает просадку, серию, vol, conf)
        size_result = self.sizer.calculate(
            portfolio=portfolio,
            entry_price=entry_price,
            sl_price=sl_price,
            confidence=confidence,
            atr_pct=atr_pct,
            win_rate=win_rate,
            avg_rr=avg_rr,
            side=direction,
        )

        if size_result.decision == SizerDecision.HALT:
            state = DrawdownState.EMERGENCY if portfolio.drawdown_pct >= self.sizer.cfg.drawdown_halt_pct else DrawdownState.DEGRADED
            return RiskStatus(
                allowed=False,
                state=state,
                reason=size_result.halt_reason,
                size_result=size_result,
            )

        state = DrawdownState.DEGRADED if size_result.decision == SizerDecision.REDUCE else DrawdownState.NORMAL

        # 6. Корреляционный фильтр для шортов
        if direction.lower() == "short":
            corr_allowed, corr_mult, corr_reason = self._check_sector_correlation_for_short(symbol)
            if not corr_allowed:
                logger.info(f"RiskManager: {corr_reason}")
                return RiskStatus(
                    allowed=False,
                    state=DrawdownState.DEGRADED,
                    reason=corr_reason,
                    size_result=size_result,
                )
            if corr_mult < 1.0:
                old_size = size_result.position_usd
                size_result.position_usd = old_size * corr_mult
                logger.info(f"RiskManager: {corr_reason} | size ${old_size:.0f} → ${size_result.position_usd:.0f}")

        # 7. Всё прошло
        return RiskStatus(
            allowed=True,
            state=state,
            reason=f"Position size: ${size_result.position_usd:,.0f} (risk {size_result.risk_pct:.2f}%)",
            size_result=size_result,
        )

    # ============================================================
    # Управление открытыми позициями
    # ============================================================

    def on_trade_opened(self, symbol: str, size_usd: float, side: str, direction: str = "long"):
        self._open_positions[symbol] = {
            "symbol": symbol,
            "side": side,
            "direction": direction,
            "size_usd": size_usd,
            "opened_at": time.time(),
        }
        self._open_count += 1

    def update_position_size(self, symbol: str, remaining_pct: float):
        """
        Обновляет размер позиции после partial close.
        remaining_pct: доля оставшейся позиции (0.75 после 25% close, 0.50 после 50%).
        """
        pos = self._open_positions.get(symbol)
        if pos is None:
            logger.warning(f"RiskManager: update_position_size — {symbol} not found")
            return
        old_size = pos["size_usd"]
        pos["size_usd"] = old_size * remaining_pct
        logger.info(
            f"RiskManager: {symbol} size updated: ${old_size:.0f} → ${pos['size_usd']:.0f} "
            f"(remaining {remaining_pct*100:.0f}%)"
        )

    def get_position_size(self, symbol: str) -> float:
        """Возвращает текущий размер позиции (с учётом partial close)."""
        pos = self._open_positions.get(symbol)
        return pos["size_usd"] if pos else 0.0

    def on_trade_closed(self, pnl_usd: float, symbol: Optional[str] = None):
        self._daily_pnl += pnl_usd
        self._open_count = max(0, self._open_count - 1)
        if symbol is not None:
            self._open_positions.pop(symbol, None)

    # ============================================================
    # Корреляция секторов (4H свечи)
    # ============================================================

    def update_candles(self, symbol: str, df: pd.DataFrame):
        """
        Обновляет кэш свечей для расчёта корреляции.
        Вызывается из DataFeed при получении 4H свечей.
        """
        self._candle_cache[symbol] = df

    def _get_symbol_sector(self, symbol: str) -> Optional[str]:
        """Возвращает сектор символа или None."""
        for sector, symbols in self.SECTOR_GROUPS.items():
            if symbol in symbols:
                return sector
        return None

    def _calculate_sector_correlation(self, sector: str, window: int = 20) -> float:
        """
        Средняя попарная корреляция returns между активами сектора.
        window: количество 4H свечей (20 свечей ≈ 3.3 дня, но 120 = 20 дней).
        Используем 120 свечей = 20 дней × 6 свечей/день.
        
        Returns: средняя корреляция (0..1), или 0.0 если данных недостаточно.
        """
        candle_window = window * 6  # 20 дней × 6 свечей (4H) = 120 свечей
        symbols = self.SECTOR_GROUPS.get(sector, [])
        
        # Собираем returns для каждого символа
        returns_dict = {}
        for sym in symbols:
            df = self._candle_cache.get(sym)
            if df is None or len(df) < candle_window:
                continue
            close = df["close"] if "close" in df.columns else df.iloc[:, 3]
            close = close.tail(candle_window).astype(float)
            ret = close.pct_change().dropna()
            if len(ret) >= candle_window - 1:
                returns_dict[sym] = ret.values

        if len(returns_dict) < 2:
            return 0.0  # недостаточно данных

        # Строим матрицу корреляций
        symbols_list = list(returns_dict.keys())
        n = len(symbols_list)
        
        # Выравниваем длины
        min_len = min(len(v) for v in returns_dict.values())
        matrix = np.array([returns_dict[s][-min_len:] for s in symbols_list])
        
        # Корреляционная матрица
        corr_matrix = np.corrcoef(matrix)
        
        # Средняя попарная корреляция (верхний треугольник без диагонали)
        pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                val = corr_matrix[i, j]
                if not np.isnan(val):
                    pairs.append(abs(val))

        return float(np.mean(pairs)) if pairs else 0.0

    def _get_all_sector_correlations(self) -> Dict[str, float]:
        """
        Возвращает корреляции всех секторов (с кэшированием на 1 час).
        """
        now = time.time()
        if now - self._corr_cache_ts < self._corr_cache_ttl and self._sector_corr_cache:
            return self._sector_corr_cache

        result = {}
        for sector in self.SECTOR_GROUPS:
            corr = self._calculate_sector_correlation(sector)
            result[sector] = corr
            if corr > 0.5:
                logger.info(f"📊 Sector {sector} correlation: {corr:.3f}")

        self._sector_corr_cache = result
        self._corr_cache_ts = now
        return result

    def _check_sector_correlation_for_short(self, symbol: str) -> Tuple[bool, float, str]:
        """
        Проверяет корреляцию сектора для шорт-сделки.
        
        Returns:
            (allowed, size_multiplier, reason)
            - allowed=False если corr > 0.85 в 2+ секторах
            - size_multiplier=0.7 если corr > 0.75 в секторе символа
            - size_multiplier=1.0 если всё ок
        """
        sector_corrs = self._get_all_sector_correlations()
        
        # Проверка 1: если corr > 0.85 в 2+ секторах → блокируем шорт
        high_corr_sectors = [s for s, c in sector_corrs.items() if c > 0.85]
        if len(high_corr_sectors) >= 2:
            return (
                False, 0.0,
                f"🚫 SHORT blocked: high correlation (>0.85) in {len(high_corr_sectors)} sectors: "
                f"{', '.join(f'{s}={sector_corrs[s]:.2f}' for s in high_corr_sectors)}"
            )

        # Проверка 2: если corr > 0.75 в секторе символа → уменьшаем размер на 30%
        symbol_sector = self._get_symbol_sector(symbol)
        if symbol_sector and sector_corrs.get(symbol_sector, 0) > 0.75:
            corr_val = sector_corrs[symbol_sector]
            return (
                True, 0.7,
                f"⚠️ SHORT size reduced 30%: {symbol_sector} correlation={corr_val:.2f} > 0.75"
            )

        return (True, 1.0, "")

    # ============================================================
    # Внутренние методы
    # ============================================================

    def _reset_daily_if_needed(self):
        now = time.time()
        if now - self._day_start >= 86400:
            logger.info(f"RiskManager: reset daily PnL (was {self._daily_pnl:+.2f}$)")
            self._daily_pnl = 0.0
            self._day_start = now

    def _check_correlation(self, symbol: str, direction: str) -> bool:
        """
        Группы корреляции: L1, DeFi, Meme. Если уже есть позиция в той же группе,
        то новый вход блокируется, чтобы не перегружать один сектор.
        Для BTC отдельная группа, но если BTC уже открыт, то остальные монеты
        считаются частично коррелированными (можно разрешить, но с уменьшением веса –
        здесь пока просто запрещаем для простоты, можно доработать).
        """
        groups = {
            "L1": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "MATICUSDT"],
            "DeFi": ["AAVEUSDT", "UNIUSDT", "LINKUSDT"],
            "Meme": ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT"],
        }
        symbol_group = None
        for group, symbols in groups.items():
            if symbol in symbols:
                symbol_group = group
                break

        if not symbol_group:
            # Неизвестная монета — разрешаем, но можно ограничить
            return True

        # Проверяем, есть ли уже позиция в этой группе
        for pos in self._open_positions.values():
            pos_symbol = pos["symbol"]
            pos_group = None
            for g, syms in groups.items():
                if pos_symbol in syms:
                    pos_group = g
                    break
            if pos_group == symbol_group:
                # Уже есть позиция в этой группе
                return False
        return True

    # ============================================================
    # Статус для Telegram
    # ============================================================

    def status_str(self, portfolio: PortfolioState) -> str:
        dd = portfolio.drawdown_pct
        daily_pct = self._daily_pnl / portfolio.balance * 100 if portfolio.balance else 0

        if dd >= self.sizer.cfg.drawdown_halt_pct:
            state = DrawdownState.EMERGENCY
        elif dd >= self.sizer.cfg.drawdown_start_pct:
            state = DrawdownState.DEGRADED
        else:
            state = DrawdownState.NORMAL

        state_emoji = {
            DrawdownState.NORMAL: "✅",
            DrawdownState.DEGRADED: "⚠️",
            DrawdownState.EMERGENCY: "🛑",
        }

        total_exposure = sum(p.get("size_usd", 0) for p in self._open_positions.values())

        return (
            f"{state_emoji[state]} RiskManager\n"
            f"Drawdown:    -{dd:.1f}% / limit -{self.sizer.cfg.drawdown_halt_pct:.0f}%\n"
            f"Дневной PnL: {daily_pct:+.1f}% / limit -{self.daily_loss_limit:.0f}%\n"
            f"Открыто:     {self._open_count}/{self.max_open_positions}\n"
            f"Экспозиция:  ${total_exposure:,.0f} / ${self.max_total_exposure_usd:,.0f}\n"
            f"Серия потерь: {portfolio.consecutive_losses}"
        )