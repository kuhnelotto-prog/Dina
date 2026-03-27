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
from typing import Optional, List, Dict

from position_sizer import PositionSizer, PortfolioState, SizerConfig, SizerDecision

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
    size_result: Optional[object] = None  # SizeResult из position_sizer

    @property
    def risk_status_str(self) -> str:
        return "OK" if self.allowed else "BLOCKED"

    @property
    def drawdown_state_str(self) -> str:
        return self.state.value


class RiskManager:
    def __init__(
        self,
        sizer_config: SizerConfig = None,
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

        logger.info(f"RiskManager init | max_pos={max_open_positions} | daily_limit={daily_loss_limit}% | total_exposure_limit=${max_total_exposure_usd}")

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
        if total_exposure + (entry_price * self.sizer.cfg.base_risk_pct / 100 * portfolio.balance) > self.max_total_exposure_usd:
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

        # 6. Всё прошло
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

    def on_trade_closed(self, pnl_usd: float):
        self._daily_pnl += pnl_usd
        self._open_count = max(0, self._open_count - 1)

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