"""
position_monitor.py — Мониторинг открытых позиций.

Отвечает за:
  - Периодический опрос позиций с биржи
  - Определение current_price из markPrice
  - Обнаружение закрытия позиций и вычисление реального PnL
  - Вызов strategist.on_trade_closed() с реальным PnL
  - Обновление баланса с биржи
  - Передача данных в TrailingManager
"""

import asyncio
import logging
import time
from typing import Dict, Optional

from trailing_manager import TrailingManager
import event_logger

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Фоновый монитор позиций.
    Запускается как asyncio task из Orchestrator.
    """

    def __init__(
        self,
        executor,
        trailing_manager: TrailingManager,
        portfolio,
        risk_manager,
        strategist_long=None,
        strategist_short=None,
        bot=None,
        check_interval: int = 10,
        balance_update_interval: int = 30,  # каждые 30 итераций ≈ 5 мин
    ):
        self.executor = executor
        self.trailing = trailing_manager
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.strategist_long = strategist_long
        self.strategist_short = strategist_short
        self.bot = bot

        self._check_interval = check_interval
        self._balance_update_interval = balance_update_interval
        self._running = False
        self._balance_counter = 0

        # Последнее известное состояние позиций: symbol -> dict
        self._last_known: Dict[str, dict] = {}

        # VaR monitoring
        self._var_limit_triggered: bool = False
        self._var_check_counter: int = 0
        self._var_check_interval: int = 30  # каждые 30 итераций ≈ 5 мин

    async def run(self):
        """Главный цикл монитора."""
        self._running = True
        logger.info("🔍 PositionMonitor запущен")

        while self._running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.warning(f"⚠ PositionMonitor: ошибка: {exc}")
            await asyncio.sleep(self._check_interval)

    def stop(self):
        self._running = False
        logger.info("PositionMonitor остановлен")

    async def _cycle(self):
        """Один цикл мониторинга."""
        # Обновление баланса
        self._balance_counter += 1
        if self._balance_counter % self._balance_update_interval == 0:
            await self._update_balance()

        # Получаем позиции с биржи
        positions = await self._get_positions()
        current_symbols = {p["symbol"] for p in positions}
        last_symbols = set(self._last_known.keys())

        # ✅ Новые позиции
        for symbol in current_symbols - last_symbols:
            pos = next(p for p in positions if p["symbol"] == symbol)
            await self._on_position_opened(pos)

        # ❌ Закрытые позиции
        for symbol in last_symbols - current_symbols:
            old_pos = self._last_known[symbol]
            await self._on_position_closed(old_pos)

        # Обновляем last_known
        self._last_known = {p["symbol"]: p for p in positions}

        # Трейлинг-стоп для каждой открытой позиции
        for pos in positions:
            await self._run_trailing(pos)

        # VaR проверка (каждые ~5 минут)
        self._var_check_counter += 1
        if self._var_check_counter % self._var_check_interval == 0:
            await self._check_portfolio_var()

    # ──────────────────────────────────────────────
    # Обработка событий
    # ──────────────────────────────────────────────

    async def _on_position_opened(self, pos: dict):
        """Новая позиция обнаружена на бирже."""
        symbol = pos["symbol"]
        side = pos.get("side", "long")
        size = pos.get("size", 0)
        entry = pos.get("entry_price", 0)
        initial_sl = pos.get("initial_sl") or pos.get("current_sl") or 0.0

        logger.info(f"✅ Новая позиция: {side} {symbol} {size} @ {entry}")

        # Регистрируем в TrailingManager
        if initial_sl > 0:
            self.trailing.register_position(symbol, initial_sl)

        # Telegram
        if self.bot:
            await self.bot._send(
                f"✅ Открыта позиция\n{side} {symbol}\nРазмер: {size}\nВход: {entry:.2f}",
                priority="info"
            )

        # Event log
        event_logger.position_opened(
            symbol=symbol,
            side=side,
            size=size,
            entry=entry,
            sl=initial_sl,
        )

    async def _on_position_closed(self, old_pos: dict):
        """Позиция исчезла с биржи — значит закрылась."""
        symbol = old_pos["symbol"]
        side = old_pos.get("side", "long")
        entry_price = old_pos.get("entry_price", 0)
        size = old_pos.get("size", 0)
        trade_id = old_pos.get("trade_id", "")

        # Вычисляем PnL
        # Берём последнюю известную markPrice как exit_price
        exit_price = old_pos.get("mark_price") or old_pos.get("markPrice") or entry_price

        # Используем актуальный размер из risk_manager (учитывает partial close)
        # Если risk_manager не знает — fallback на оригинальный size
        remaining_size_usd = self.risk_manager.get_position_size(symbol)
        if remaining_size_usd <= 0:
            remaining_size_usd = size * entry_price if entry_price > 0 else 0.0

        if entry_price > 0 and remaining_size_usd > 0:
            if side.lower() == "long":
                pnl_pct = (exit_price - entry_price) / entry_price * 100
            else:
                pnl_pct = (entry_price - exit_price) / entry_price * 100
            pnl_usd = remaining_size_usd * pnl_pct / 100
        else:
            pnl_pct = 0.0
            pnl_usd = 0.0

        logger.info(
            f"❌ Позиция закрыта: {side} {symbol} | "
            f"entry={entry_price:.2f} exit={exit_price:.2f} | "
            f"PnL: {pnl_usd:+.2f}$ ({pnl_pct:+.2f}%)"
        )

        # Убираем из TrailingManager
        self.trailing.unregister_position(symbol)

        # Определяем причину закрытия
        trailing_state = self.trailing.get_state(symbol)
        if trailing_state.get("trailing_step", 0) >= 4:
            reason = "trailing_tp"
        elif pnl_usd < 0:
            reason = "sl_hit"
        else:
            reason = "tp_hit"

        # Вызываем strategist.on_trade_closed() с реальным PnL
        strategist = self._get_strategist_for_side(side)
        if strategist:
            await strategist.on_trade_closed(
                trade_id=trade_id,
                symbol=symbol,
                exit_price=exit_price,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                reason=reason,
            )
        else:
            # Fallback: обновляем portfolio и risk_manager напрямую
            self.portfolio.update(pnl_usd)
            self.risk_manager.on_trade_closed(pnl_usd, symbol)
            logger.warning(f"PositionMonitor: no strategist for {side}, updated portfolio/risk directly")

        # Telegram
        if self.bot:
            emoji = "💚" if pnl_usd >= 0 else "🔴"
            await self.bot._send(
                f"{emoji} Позиция закрыта\n{side} {symbol}\n"
                f"PnL: {pnl_usd:+.2f}$ ({pnl_pct:+.2f}%)\n"
                f"Причина: {reason}",
                priority="info"
            )

        # Event log
        event_logger.position_closed(symbol=symbol, side=side, pnl=pnl_usd)

    # ──────────────────────────────────────────────
    # Трейлинг
    # ──────────────────────────────────────────────

    async def _run_trailing(self, pos: dict):
        """Запускает трейлинг для одной позиции."""
        symbol = pos["symbol"]
        side = pos.get("side", "long")
        entry_price = pos.get("entry_price", 0)
        initial_sl = pos.get("initial_sl") or pos.get("current_sl") or 0.0

        # current_price берём из markPrice (не из pos["current_price"])
        current_price = float(
            pos.get("markPrice")
            or pos.get("mark_price")
            or pos.get("current_price")
            or 0
        )

        if current_price <= 0 or entry_price <= 0 or initial_sl <= 0:
            return

        await self.trailing.update(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            initial_sl=initial_sl,
            current_price=current_price,
        )

    # ──────────────────────────────────────────────
    # Вспомогательные
    # ──────────────────────────────────────────────

    async def _get_positions(self) -> list:
        """Получает позиции с биржи."""
        try:
            return await self.executor.get_open_positions() or []
        except Exception as exc:
            logger.error(f"PositionMonitor: не удалось получить позиции: {exc}")
            return []

    async def _update_balance(self):
        """Обновляет баланс с биржи."""
        try:
            new_balance = await self.executor.get_balance()
            old_balance = self.portfolio.balance
            self.portfolio.balance = new_balance
            if new_balance > self.portfolio.peak_balance:
                self.portfolio.peak_balance = new_balance
            logger.info(f"💰 Баланс обновлён: ${old_balance:.2f} → ${new_balance:.2f}")
        except Exception as e:
            logger.warning(f"Не удалось обновить баланс: {e}")

    async def _check_portfolio_var(self):
        """
        Проверяет портфельный VaR и автоматически снижает/восстанавливает риск.
        
        Если VaR > 10% баланса → уменьшить max_risk_pct вдвое.
        Если VaR вернулся ниже порога → восстановить исходный max_risk_pct.
        """
        try:
            exceeded, var_usd = self.risk_manager.check_var_limit(
                portfolio=self.portfolio,
                atr_pct_by_symbol=None,  # используем дефолт 1.5%
                var_limit_pct=0.10,
            )

            if exceeded and not self._var_limit_triggered:
                # VaR превышен — снижаем риск
                self._var_limit_triggered = True
                self.risk_manager.apply_var_reduction()
                logger.warning(
                    f"📉 VaR limit triggered: ${var_usd:.0f} > "
                    f"${self.portfolio.balance * 0.10:.0f} (10% of balance)"
                )
                if self.bot:
                    await self.bot._send(
                        f"⚠️ VaR limit exceeded!\n"
                        f"VaR: ${var_usd:.0f}\n"
                        f"Limit: ${self.portfolio.balance * 0.10:.0f}\n"
                        f"max_risk_pct reduced by 50%",
                        priority="warning"
                    )

            elif not exceeded and self._var_limit_triggered:
                # VaR вернулся в норму — восстанавливаем
                self._var_limit_triggered = False
                self.risk_manager.restore_var_risk()
                logger.info(
                    f"📈 VaR limit cleared: ${var_usd:.0f} < "
                    f"${self.portfolio.balance * 0.10:.0f}"
                )
                if self.bot:
                    await self.bot._send(
                        f"✅ VaR limit cleared\n"
                        f"VaR: ${var_usd:.0f}\n"
                        f"max_risk_pct restored",
                        priority="info"
                    )

        except Exception as e:
            logger.warning(f"VaR check error: {e}")

    def _get_strategist_for_side(self, side: str):
        """Возвращает нужный strategist по стороне позиции."""
        side_lower = side.lower()
        if side_lower == "long" and self.strategist_long:
            return self.strategist_long
        elif side_lower == "short" and self.strategist_short:
            return self.strategist_short
        # Fallback: если не знаем сторону, пробуем long
        return self.strategist_long