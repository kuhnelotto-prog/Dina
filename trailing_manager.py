"""
trailing_manager.py — Трейлинг-стоп по 4 шагам.

Шаги:
  1. +0.5R → стоп на breakeven
  2. +1.0R → закрыть 25%, стоп на +0.5R
  3. +1.5R → закрыть ещё 25%, стоп на +1.0R
  4. +2.5R → закрыть всё
"""

import logging
from typing import Optional, Dict

import event_logger

logger = logging.getLogger(__name__)


class TrailingManager:
    """
    Управляет трейлинг-стопом для открытых позиций.
    Вызывается из PositionMonitor на каждом тике.
    """

    def __init__(self, executor, bot=None, risk_manager=None):
        self.executor = executor
        self.bot = bot
        self.risk_manager = risk_manager
        # Состояние трейлинга: symbol -> {"current_sl": float, "trailing_step": int, "remaining_pct": float}
        self._state: Dict[str, dict] = {}

    def register_position(self, symbol: str, initial_sl: float):
        """Регистрирует новую позицию для трейлинга."""
        self._state[symbol] = {
            "current_sl": initial_sl,
            "trailing_step": 0,
        }
        logger.info(f"TrailingManager: зарегистрирована {symbol} | SL={initial_sl:.4f}")

    def unregister_position(self, symbol: str):
        """Убирает позицию из трейлинга."""
        self._state.pop(symbol, None)

    def get_state(self, symbol: str) -> dict:
        """Возвращает текущее состояние трейлинга."""
        return self._state.get(symbol, {"current_sl": 0, "trailing_step": 0})

    async def update(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        initial_sl: float,
        current_price: float,
    ) -> bool:
        """
        Проверяет и обновляет трейлинг-стоп.
        
        Args:
            symbol: Символ
            side: "long" или "short"
            entry_price: Цена входа
            initial_sl: Начальный стоп-лосс
            current_price: Текущая рыночная цена (markPrice)
            
        Returns:
            True если позиция была полностью закрыта (шаг 4)
        """
        state = self._state.get(symbol)
        if state is None:
            # Авто-регистрация если не было
            self.register_position(symbol, initial_sl)
            state = self._state[symbol]

        current_sl = state["current_sl"]
        step = state["trailing_step"]

        # Считаем R (risk units)
        risk = abs(entry_price - initial_sl)
        if risk == 0:
            return False

        side_lower = side.lower()
        if side_lower == "long":
            r = (current_price - entry_price) / risk
        else:
            r = (entry_price - current_price) / risk

        new_sl = current_sl
        new_step = step
        fully_closed = False

        # Шаг 1 — breakeven (+0.5R)
        if step < 1 and r >= 0.5:
            new_sl = entry_price
            new_step = 1
            logger.info(f"🔒 {symbol} Шаг 1: стоп → breakeven {new_sl:.4f}")
            if self.bot:
                await self.bot._send(f"🔒 {symbol} стоп перенесён на вход ({new_sl:.4f})")
            event_logger.trailing_stop_moved(symbol, current_sl, new_sl, step=1)

        # Шаг 2 — закрыть 25%, стоп на +0.5R
        elif step < 2 and r >= 1.0:
            if side_lower == "long":
                new_sl = entry_price + risk * 0.5
            else:
                new_sl = entry_price - risk * 0.5
            new_step = 2
            if self.executor:
                try:
                    await self.executor.partial_close(symbol, side_lower, pct=0.25)
                except Exception as e:
                    logger.error(f"TrailingManager: partial_close failed {symbol}: {e}")
            logger.info(f"💰 {symbol} Шаг 2: закрыто 25%, стоп → {new_sl:.4f}")
            if self.bot:
                await self.bot._send(f"💰 {symbol} закрыто 25% позиции\nСтоп: {new_sl:.4f}")
            event_logger.partial_close(symbol, pct=25, price=current_price, step=2)
            event_logger.trailing_stop_moved(symbol, current_sl, new_sl, step=2)
            # Синхронизируем risk_manager: осталось 75% позиции
            if self.risk_manager:
                self.risk_manager.update_position_size(symbol, remaining_pct=0.75)

        # Шаг 3 — закрыть ещё 25%, стоп на +1.0R
        elif step < 3 and r >= 1.5:
            if side_lower == "long":
                new_sl = entry_price + risk * 1.0
            else:
                new_sl = entry_price - risk * 1.0
            new_step = 3
            if self.executor:
                try:
                    await self.executor.partial_close(symbol, side_lower, pct=0.25)
                except Exception as e:
                    logger.error(f"TrailingManager: partial_close failed {symbol}: {e}")
            logger.info(f"💰 {symbol} Шаг 3: закрыто ещё 25%, стоп → {new_sl:.4f}")
            if self.bot:
                await self.bot._send(f"💰 {symbol} закрыто ещё 25% позиции\nСтоп: {new_sl:.4f}")
            event_logger.partial_close(symbol, pct=25, price=current_price, step=3)
            event_logger.trailing_stop_moved(symbol, current_sl, new_sl, step=3)
            # Синхронизируем risk_manager: осталось 50% позиции (75% * 2/3 ≈ 50% от оригинала)
            if self.risk_manager:
                self.risk_manager.update_position_size(symbol, remaining_pct=2/3)

        # Шаг 4 — закрыть всё (+2.5R)
        elif step < 4 and r >= 2.5:
            new_step = 4
            fully_closed = True
            if self.executor:
                try:
                    await self.executor.close_position(symbol, side_lower)
                except Exception as e:
                    logger.error(f"TrailingManager: close_position failed {symbol}: {e}")
            logger.info(f"🏁 {symbol} Шаг 4: закрыта вся позиция на +2.5R")
            if self.bot:
                await self.bot._send(f"🏁 {symbol} позиция закрыта полностью (+2.5R)")

        # Сохраняем новое состояние
        if new_step != step:
            state["current_sl"] = new_sl
            state["trailing_step"] = new_step

            # Двигаем реальный стоп на бирже (кроме шага 4 — позиция уже закрыта)
            if new_step < 4 and self.executor:
                try:
                    await self.executor.move_stop_loss(symbol, side_lower, new_sl)
                except Exception as e:
                    logger.error(f"TrailingManager: move_stop_loss failed {symbol}: {e}")

        return fully_closed