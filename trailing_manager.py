"""
trailing_manager.py — 4-этапный трейлинг-стоп на ATR.

Этапы:
  1. +0.5×ATR → стоп на breakeven
  2. +1.0×ATR → закрыть 25%, стоп на +0.5×ATR
  3. +1.5×ATR → закрыть ещё 25%, стоп на +1.0×ATR
  4. +2.0×ATR → закрыть всё

ATR берётся из сигнала при входе (atr_value) и фиксируется на весь трейд.
"""

import logging
from typing import Optional, Dict

import event_logger

logger = logging.getLogger(__name__)


# Конфигурация этапов трейлинга (в единицах ATR)
TRAILING_STAGES = [
    {"stage": 1, "activation_atr": 0.5, "sl_atr": 0.0,  "partial_close_pct": 0.0,  "description": "breakeven"},
    {"stage": 2, "activation_atr": 1.0, "sl_atr": 0.5,  "partial_close_pct": 0.25, "description": "close 25%"},
    {"stage": 3, "activation_atr": 1.5, "sl_atr": 1.0,  "partial_close_pct": 0.25, "description": "close 25%"},
    {"stage": 4, "activation_atr": 2.0, "sl_atr": None,  "partial_close_pct": 1.0,  "description": "close all"},
]


class TrailingManager:
    """
    Управляет трейлинг-стопом для открытых позиций.
    Вызывается из PositionMonitor на каждом тике.
    
    Использует ATR (Average True Range) вместо R (risk units) для определения
    уровней активации и подтяжки стопа.
    """

    def __init__(self, executor, bot=None, risk_manager=None):
        self.executor = executor
        self.bot = bot
        self.risk_manager = risk_manager
        # Состояние трейлинга: symbol -> {current_sl, trailing_step, atr_value}
        self._state: Dict[str, dict] = {}

    def register_position(self, symbol: str, initial_sl: float, atr_value: float = 0.0):
        """
        Регистрирует новую позицию для трейлинга.
        
        Args:
            symbol: Символ
            initial_sl: Начальный стоп-лосс
            atr_value: ATR на момент входа (фиксируется на весь трейд)
        """
        self._state[symbol] = {
            "current_sl": initial_sl,
            "trailing_step": 0,
            "atr_value": atr_value,
        }
        logger.info(f"TrailingManager: зарегистрирована {symbol} | SL={initial_sl:.4f} ATR={atr_value:.4f}")

    def unregister_position(self, symbol: str):
        """Убирает позицию из трейлинга."""
        self._state.pop(symbol, None)

    def get_state(self, symbol: str) -> dict:
        """Возвращает текущее состояние трейлинга."""
        return self._state.get(symbol, {"current_sl": 0, "trailing_step": 0, "atr_value": 0})

    async def update(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        initial_sl: float,
        current_price: float,
        atr_value: float = 0.0,
    ) -> bool:
        """
        Проверяет и обновляет трейлинг-стоп по 4-этапной ATR-логике.
        
        Args:
            symbol: Символ
            side: "long" или "short"
            entry_price: Цена входа
            initial_sl: Начальный стоп-лосс
            current_price: Текущая рыночная цена (markPrice)
            atr_value: ATR (если 0 — используем сохранённый при регистрации)
            
        Returns:
            True если позиция была полностью закрыта (шаг 4)
        """
        state = self._state.get(symbol)
        if state is None:
            # Авто-регистрация если не было
            self.register_position(symbol, initial_sl, atr_value)
            state = self._state[symbol]

        current_sl = state["current_sl"]
        step = state["trailing_step"]
        
        # ATR: используем сохранённый при регистрации, или переданный
        atr = atr_value if atr_value > 0 else state.get("atr_value", 0)
        
        # Fallback: если ATR не задан, используем risk (расстояние до SL) как proxy
        if atr <= 0:
            atr = abs(entry_price - initial_sl)
        
        if atr <= 0:
            return False

        side_lower = side.lower()
        
        # Считаем PnL в единицах ATR
        if side_lower == "long":
            pnl_atr = (current_price - entry_price) / atr
        else:
            pnl_atr = (entry_price - current_price) / atr

        new_sl = current_sl
        new_step = step
        fully_closed = False

        # Проходим по этапам
        for stage_cfg in TRAILING_STAGES:
            stage_num = stage_cfg["stage"]
            activation = stage_cfg["activation_atr"]
            
            if step >= stage_num:
                continue  # уже прошли этот этап
            
            if pnl_atr < activation:
                break  # ещё не достигли этого уровня

            # === Этап активирован ===
            
            if stage_num == 4:
                # Шаг 4 — закрыть всё
                new_step = 4
                fully_closed = True
                if self.executor:
                    try:
                        await self.executor.close_position(symbol, side_lower)
                    except Exception as e:
                        logger.error(f"TrailingManager: close_position failed {symbol}: {e}")
                logger.info(f"🏁 {symbol} Шаг 4: закрыта вся позиция на +{activation}×ATR")
                if self.bot:
                    await self.bot._send(f"🏁 {symbol} позиция закрыта полностью (+{activation}×ATR)")
                break
            
            # Вычисляем новый SL
            sl_atr_offset = stage_cfg["sl_atr"]
            if side_lower == "long":
                new_sl = entry_price + atr * sl_atr_offset
            else:
                new_sl = entry_price - atr * sl_atr_offset
            new_step = stage_num
            
            # Partial close
            partial_pct = stage_cfg["partial_close_pct"]
            if partial_pct > 0 and self.executor:
                try:
                    await self.executor.partial_close(symbol, side_lower, pct=partial_pct)
                except Exception as e:
                    logger.error(f"TrailingManager: partial_close failed {symbol}: {e}")
            
            # Логирование
            desc = stage_cfg["description"]
            if partial_pct > 0:
                logger.info(
                    f"💰 {symbol} Шаг {stage_num}: {desc} ({partial_pct*100:.0f}%), "
                    f"стоп → {new_sl:.4f} (+{sl_atr_offset}×ATR)"
                )
                if self.bot:
                    await self.bot._send(
                        f"💰 {symbol} Шаг {stage_num}: {desc}\n"
                        f"Стоп: {new_sl:.4f} (+{sl_atr_offset}×ATR)"
                    )
                event_logger.partial_close(symbol, pct=int(partial_pct*100), price=current_price, step=stage_num)
                # Синхронизируем risk_manager
                if self.risk_manager:
                    remaining = {2: 0.75, 3: 0.5625}  # после шага 2: 75%, после шага 3: 56.25% (75%×75%)
                    if stage_num in remaining:
                        self.risk_manager.update_position_size(symbol, remaining_pct=remaining[stage_num])
            else:
                logger.info(
                    f"🔒 {symbol} Шаг {stage_num}: {desc}, стоп → {new_sl:.4f}"
                )
                if self.bot:
                    await self.bot._send(f"🔒 {symbol} стоп перенесён на {new_sl:.4f} ({desc})")
            
            event_logger.trailing_stop_moved(symbol, current_sl, new_sl, step=stage_num)

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
