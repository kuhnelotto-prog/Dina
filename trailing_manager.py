"""
trailing_manager.py — P8+P34 Asymmetric Trailing Stop.

LONG (P34):
  - Step 0: DISABLED (no trailing before TP1 — give longs room to breathe)
  - TP1 at +1 ATR: close 30%, SL to entry - TSL_ATR_LONG_AFTER_TP1*ATR
  - TP2 at +2 ATR: close 30%, TSL from peak at TSL_ATR_LONG_AFTER_TP1*ATR
  - After TP1+: TSL continues from peak at TSL_ATR_LONG_AFTER_TP1*ATR

SHORT (P8 standard):
  - TP1 at +1 ATR: close 30%, SL to breakeven + 0.5*ATR
  - TP2 at +2 ATR: close 30%, TSL from peak at TSL_ATR_SHORT*ATR
  - After TP1+: TSL continues from peak at TSL_ATR_SHORT*ATR

ATR берётся из сигнала при входе (atr_value) и фиксируется на весь трейд.
Конфигурация: из config.py (SL_ATR_MULT_LONG, TSL_ATR_LONG_AFTER_TP1, TSL_ATR_SHORT).
"""

import logging
from typing import Optional, Dict

import event_logger
from config import (
    SL_ATR_MULT_LONG,
    SL_ATR_MULT_SHORT,
    TSL_ATR_LONG_AFTER_TP1,
    TSL_ATR_SHORT,
)

logger = logging.getLogger(__name__)


class TrailingManager:
    """
    Управляет трейлинг-стопом для открытых позиций.
    Вызывается из PositionMonitor на каждом тике.
    
    P8+P34: Asymmetric LONG/SHORT trailing logic.
    Синхронизировано с backtester.py _apply_trailing_4step().
    """

    # Partial close percentages (synced with backtester)
    TP1_CLOSE_PCT = 0.30  # close 30% of original at TP1
    TP2_CLOSE_PCT = 0.30  # close 30% of original at TP2

    def __init__(self, executor, bot=None, risk_manager=None):
        self.executor = executor
        self.bot = bot
        self.risk_manager = risk_manager
        # Состояние трейлинга: symbol -> dict
        self._state: Dict[str, dict] = {}

    def register_position(self, symbol: str, initial_sl: float, atr_value: float = 0.0,
                          side: str = "long", entry_price: float = 0.0):
        """
        Регистрирует новую позицию для трейлинга.
        
        Args:
            symbol: Символ
            initial_sl: Начальный стоп-лосс
            atr_value: ATR на момент входа (фиксируется на весь трейд)
            side: "long" или "short"
            entry_price: Цена входа (нужна для peak tracking)
        """
        self._state[symbol] = {
            "current_sl": initial_sl,
            "trailing_step": 0,
            "atr_value": atr_value,
            "remaining_pct": 1.0,  # доля оригинальной позиции
            "side": side.lower(),
            "entry_price": entry_price,
            "peak_price": entry_price,  # для TSL от пика
        }
        logger.info(
            f"TrailingManager: зарегистрирована {symbol} {side} | "
            f"SL={initial_sl:.4f} ATR={atr_value:.4f}"
        )

    def unregister_position(self, symbol: str):
        """Убирает позицию из трейлинга."""
        self._state.pop(symbol, None)

    def get_state(self, symbol: str) -> dict:
        """Возвращает текущее состояние трейлинга."""
        return self._state.get(symbol, {
            "current_sl": 0, "trailing_step": 0, "atr_value": 0,
            "remaining_pct": 1.0, "side": "long", "peak_price": 0,
        })

    async def update(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        initial_sl: float,
        current_price: float,
        atr_value: float = 0.0,
        current_step: Optional[int] = None,
        remaining_pct: Optional[float] = None,
    ) -> bool:
        """
        P8+P34 Asymmetric trailing logic (synced with backtester.py).
        
        Args:
            symbol: Символ
            side: "long" или "short"
            entry_price: Цена входа
            initial_sl: Начальный стоп-лосс
            current_price: Текущая рыночная цена (markPrice)
            atr_value: ATR (если 0 — используем сохранённый при регистрации)
            current_step: Текущий шаг (синхронизация после рестарта)
            remaining_pct: Текущий остаток (синхронизация после рестарта)
            
        Returns:
            True если позиция была полностью закрыта
        """
        state = self._state.get(symbol)
        if state is None:
            # Авто-регистрация если не было
            self.register_position(symbol, initial_sl, atr_value, side, entry_price)
            state = self._state[symbol]
            if current_step is not None:
                state["trailing_step"] = current_step
            if remaining_pct is not None:
                state["remaining_pct"] = remaining_pct

        # Синхронизация шага (важно при рестарте)
        if current_step is not None and current_step > state["trailing_step"]:
            state["trailing_step"] = current_step
        if remaining_pct is not None:
            state["remaining_pct"] = remaining_pct

        current_sl = state["current_sl"]
        step = state["trailing_step"]
        side_lower = side.lower()

        # Обновляем side/entry_price если не было при регистрации
        state["side"] = side_lower
        if state.get("entry_price", 0) == 0:
            state["entry_price"] = entry_price

        # ATR: используем сохранённый при регистрации, или переданный
        atr = atr_value if atr_value > 0 else state.get("atr_value", 0)

        # Fallback: если ATR не задан, используем distance до SL / multiplier
        if atr <= 0:
            if side_lower == "long":
                atr = abs(entry_price - initial_sl) / SL_ATR_MULT_LONG
            else:
                atr = abs(initial_sl - entry_price) / SL_ATR_MULT_SHORT

        if atr <= 0:
            return False

        # ── Peak tracking ──
        peak_price = state.get("peak_price", entry_price)
        if side_lower == "long":
            if current_price > peak_price:
                peak_price = current_price
                state["peak_price"] = peak_price
        else:
            if current_price < peak_price or peak_price == entry_price:
                peak_price = current_price
                state["peak_price"] = peak_price

        # Calculate ATR move from entry
        if side_lower == "long":
            atr_move = (current_price - entry_price) / atr
        else:
            atr_move = (entry_price - current_price) / atr

        new_sl = current_sl
        new_step = step
        fully_closed = False

        # ══════════════════════════════════════
        # LONG positions — P34 asymmetric logic
        # ══════════════════════════════════════
        if side_lower == "long":
            # Step 0: DISABLED for LONG (P34) — no trailing before TP1

            # TP1 at +1 ATR: close 30%, SL to entry - TSL_ATR_LONG_AFTER_TP1*ATR
            if step < 1 and atr_move >= 1.0:
                new_step = 1
                new_sl = entry_price - TSL_ATR_LONG_AFTER_TP1 * atr
                if new_sl > current_sl:
                    current_sl = new_sl
                pct_of_current = self.TP1_CLOSE_PCT / max(state.get("remaining_pct", 1.0), 0.01)
                pct_of_current = min(pct_of_current, 1.0)
                if self.executor:
                    try:
                        await self.executor.partial_close(symbol, side_lower, pct=pct_of_current)
                    except Exception as e:
                        logger.error(f"TrailingManager: partial_close failed {symbol}: {e}")
                state["remaining_pct"] = state.get("remaining_pct", 1.0) - self.TP1_CLOSE_PCT
                if self.risk_manager:
                    self.risk_manager.update_position_size(symbol, remaining_pct=state.get("remaining_pct", 1.0))
                logger.info(
                    f"📈 {symbol} LONG TP1: close 30% at +1 ATR, "
                    f"SL→entry-{TSL_ATR_LONG_AFTER_TP1}ATR={new_sl:.4f}"
                )
                if self.bot:
                    await self.bot._send(
                        f"📈 {symbol} LONG TP1: +1 ATR\n"
                        f"SL: {new_sl:.4f}\n"
                        f"Remaining: {state['remaining_pct']*100:.0f}%"
                    )
                event_logger.trailing_stop_moved(symbol, current_sl, new_sl, step=1)

            # TP2 at +2 ATR: close 30%, TSL from peak
            if step < 2 and atr_move >= 2.0:
                new_step = 2
                # Close 30% of original = fraction of remaining
                remaining = state.get("remaining_pct", 0.7)
                if remaining > 0:
                    pct_of_current = self.TP2_CLOSE_PCT / max(remaining, 0.01)
                    pct_of_current = min(pct_of_current, 1.0)
                    if self.executor:
                        try:
                            await self.executor.partial_close(symbol, side_lower, pct=pct_of_current)
                        except Exception as e:
                            logger.error(f"TrailingManager: partial_close failed {symbol}: {e}")
                    state["remaining_pct"] = remaining - self.TP2_CLOSE_PCT
                    if self.risk_manager:
                        self.risk_manager.update_position_size(symbol, remaining_pct=state.get("remaining_pct", 1.0))
                new_sl = peak_price - TSL_ATR_LONG_AFTER_TP1 * atr
                if new_sl > current_sl:
                    current_sl = new_sl
                logger.info(
                    f"📈 {symbol} LONG TP2: close 30% at +2 ATR, "
                    f"TSL from peak={peak_price:.4f}"
                )
                if self.bot:
                    await self.bot._send(
                        f"📈 {symbol} LONG TP2: +2 ATR\n"
                        f"TSL: {new_sl:.4f} (from peak {peak_price:.4f})\n"
                        f"Remaining: {state.get('remaining_pct', 0.4)*100:.0f}%"
                    )
                event_logger.trailing_stop_moved(symbol, current_sl, new_sl, step=2)

            # After TP1+: continuous TSL from peak
            if new_step >= 1 or state.get("trailing_step", 0) >= 1:
                tsl = peak_price - TSL_ATR_LONG_AFTER_TP1 * atr
                if tsl > current_sl:
                    current_sl = tsl
                    new_sl = tsl

        # ══════════════════════════════════════
        # SHORT positions — P8 standard logic
        # ══════════════════════════════════════
        else:
            # TP1 at +1 ATR: close 30%, SL to breakeven + 0.5 ATR
            if step < 1 and atr_move >= 1.0:
                new_step = 1
                new_sl = entry_price + 0.5 * atr
                if new_sl < current_sl:
                    current_sl = new_sl
                pct_of_current = self.TP1_CLOSE_PCT / max(state.get("remaining_pct", 1.0), 0.01)
                pct_of_current = min(pct_of_current, 1.0)
                if self.executor:
                    try:
                        await self.executor.partial_close(symbol, side_lower, pct=pct_of_current)
                    except Exception as e:
                        logger.error(f"TrailingManager: partial_close failed {symbol}: {e}")
                state["remaining_pct"] = state.get("remaining_pct", 1.0) - self.TP1_CLOSE_PCT
                if self.risk_manager:
                    self.risk_manager.update_position_size(symbol, remaining_pct=state.get("remaining_pct", 1.0))
                logger.info(
                    f"📉 {symbol} SHORT TP1: close 30% at +1 ATR, "
                    f"SL→breakeven+0.5ATR={new_sl:.4f}"
                )
                if self.bot:
                    await self.bot._send(
                        f"📉 {symbol} SHORT TP1: +1 ATR\n"
                        f"SL: {new_sl:.4f}\n"
                        f"Remaining: {state['remaining_pct']*100:.0f}%"
                    )
                event_logger.trailing_stop_moved(symbol, current_sl, new_sl, step=1)

            # TP2 at +2 ATR: close 30%, TSL from peak at 1.5 ATR
            if step < 2 and atr_move >= 2.0:
                new_step = 2
                remaining = state.get("remaining_pct", 0.7)
                if remaining > 0:
                    pct_of_current = self.TP2_CLOSE_PCT / max(remaining, 0.01)
                    pct_of_current = min(pct_of_current, 1.0)
                    if self.executor:
                        try:
                            await self.executor.partial_close(symbol, side_lower, pct=pct_of_current)
                        except Exception as e:
                            logger.error(f"TrailingManager: partial_close failed {symbol}: {e}")
                    state["remaining_pct"] = remaining - self.TP2_CLOSE_PCT
                    if self.risk_manager:
                        self.risk_manager.update_position_size(symbol, remaining_pct=state.get("remaining_pct", 1.0))
                new_sl = peak_price + TSL_ATR_SHORT * atr
                if new_sl < current_sl:
                    current_sl = new_sl
                logger.info(
                    f"📉 {symbol} SHORT TP2: close 30% at +2 ATR, "
                    f"TSL from peak={peak_price:.4f}"
                )
                if self.bot:
                    await self.bot._send(
                        f"📉 {symbol} SHORT TP2: +2 ATR\n"
                        f"TSL: {new_sl:.4f} (from peak {peak_price:.4f})\n"
                        f"Remaining: {state.get('remaining_pct', 0.4)*100:.0f}%"
                    )
                event_logger.trailing_stop_moved(symbol, current_sl, new_sl, step=2)

            # After TP1+: continuous TSL from peak
            if new_step >= 1 or state.get("trailing_step", 0) >= 1:
                tsl = peak_price + TSL_ATR_SHORT * atr
                if tsl < current_sl:
                    current_sl = tsl
                    new_sl = tsl

        # ── Сохраняем новое состояние ──
        if new_step != step or new_sl != current_sl:
            state["current_sl"] = new_sl if new_sl != current_sl else current_sl
            # Actually we need to always update if something changed
            state["current_sl"] = current_sl
            state["trailing_step"] = max(new_step, step)

            # Двигаем реальный стоп на бирже
            if self.executor:
                try:
                    await self.executor.move_stop_loss(symbol, side_lower, current_sl)
                except Exception as e:
                    logger.error(f"TrailingManager: move_stop_loss failed {symbol}: {e}")

        return fully_closed