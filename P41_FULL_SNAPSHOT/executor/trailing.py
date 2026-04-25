"""
executor/trailing.py

Трейлинг-стоп логика из bitget_executor:
  - move_stop_loss
  - _monitor_loop (динамический таймаут, трейлинг)
  - _get_dynamic_timeout_hours
  - save/restore/clear trailing state в БД
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Any

import aiosqlite

logger = logging.getLogger(__name__)


class ExecutorTrailingManager:
    """Управление трейлинг-стопами и мониторинг позиций."""

    def __init__(self, api_client, cfg, positions: Dict, position_ages: Dict, db_path: str):
        """
        Args:
            api_client: BitgetAPIClient instance
            cfg: ExecutorConfig
            positions: shared dict of PositionInfo
            position_ages: shared dict symbol -> age in checks
            db_path: path to SQLite DB
        """
        self.api = api_client
        self.cfg = cfg
        self._positions = positions
        self._position_ages = position_ages
        self._db_path = db_path

    # ============================================================
    # Move stop loss
    # ============================================================

    async def move_stop_loss(self, symbol: str, side: str, new_sl: float):
        """Переставить стоп-ордер на новую цену."""
        if self.cfg.dry_run:
            logger.info(f"[DRY] move_sl {symbol} → {new_sl}")
            if symbol in self._positions:
                self._positions[symbol].current_sl = new_sl
            return

        await self.api.cancel_sl_order(symbol)
        await self.api.place_sl(symbol, side, self._positions[symbol].size, new_sl)
        if symbol in self._positions:
            self._positions[symbol].current_sl = new_sl

    # ============================================================
    # Dynamic timeout
    # ============================================================

    def get_dynamic_timeout_hours(self, pos, current_price: float) -> float:
        """
        Возвращает таймаут в часах в зависимости от PnL в ATR.
        
        - PnL < 1 ATR → base_timeout (48h)
        - PnL >= 1 ATR → mid_timeout (72h)
        - PnL >= 2 ATR → max_timeout (96h)
        """
        from bitget_executor import PositionSide

        entry = pos.avg_price
        risk = abs(entry - pos.initial_sl) if pos.initial_sl > 0 else 0

        if risk <= 0 or entry <= 0:
            return self.cfg.base_timeout_hours

        if pos.side == PositionSide.LONG:
            pnl_price = current_price - entry
        else:
            pnl_price = entry - current_price

        pnl_atr = pnl_price / risk

        if pnl_atr >= self.cfg.timeout_atr_max:
            return self.cfg.max_timeout_hours
        elif pnl_atr >= self.cfg.timeout_atr_mid:
            return self.cfg.mid_timeout_hours
        else:
            return self.cfg.base_timeout_hours

    # ============================================================
    # Monitor loop
    # ============================================================

    async def monitor_loop(self, close_position_fn):
        """
        Запускается в отдельной задаче для управления позициями.
        
        Args:
            close_position_fn: async callable(symbol, reason) для закрытия позиции
        """
        from bitget_executor import PositionSide

        while True:
            await asyncio.sleep(10)
            try:
                for symbol, pos in list(self._positions.items()):
                    if not pos.is_open:
                        continue

                    price = await self.api.get_last_price(symbol)
                    if not price:
                        continue

                    # Возраст позиции
                    age = self._position_ages.get(symbol, 0) + 1
                    self._position_ages[symbol] = age

                    # Динамический таймаут
                    timeout_hours = self.get_dynamic_timeout_hours(pos, price)
                    timeout_checks = int(timeout_hours * 3600 / 10)

                    if age > timeout_checks:
                        if pos.side == PositionSide.LONG:
                            pnl_pct = (price - pos.avg_price) / pos.avg_price * 100
                        else:
                            pnl_pct = (pos.avg_price - price) / pos.avg_price * 100

                        if pnl_pct < self.cfg.min_expected_pnl_pct:
                            logger.info(
                                f"Position {symbol} dynamic timeout "
                                f"(age={age}, limit={timeout_checks}, "
                                f"timeout={timeout_hours:.0f}h, PnL={pnl_pct:.2f}%), closing"
                            )
                            await close_position_fn(symbol, reason=f"timeout_{timeout_hours:.0f}h")
                            continue
                        else:
                            logger.debug(
                                f"Position {symbol} past timeout but PnL={pnl_pct:.2f}% > "
                                f"{self.cfg.min_expected_pnl_pct}%, keeping"
                            )

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

    # ============================================================
    # Trailing state persistence (DB)
    # ============================================================

    async def save_trailing_state(self, symbol: str, activated: bool,
                                  trailing_stop: float, stage: int,
                                  plan_order_id: str):
        """Сохраняет состояние трейлинга в БД."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO active_trailing
                       (symbol, activated, trailing_stop, stage, plan_order_id, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (symbol, activated, trailing_stop, stage, plan_order_id, time.time())
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to save trailing state: {e}")

    async def restore_trailing_state(self, symbol: str) -> Optional[dict]:
        """Восстанавливает состояние трейлинга из БД."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute(
                    "SELECT activated, trailing_stop, stage, plan_order_id FROM active_trailing WHERE symbol = ?",
                    (symbol,)
                )
                row = await cursor.fetchone()
                if row:
                    return {
                        "activated": bool(row[0]),
                        "trailing_stop": float(row[1]),
                        "stage": int(row[2]),
                        "plan_order_id": row[3],
                    }
        except Exception as e:
            logger.warning(f"Failed to restore trailing state: {e}")
        return None

    async def clear_trailing_state(self, symbol: str):
        """Удаляет состояние трейлинга из БД."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("DELETE FROM active_trailing WHERE symbol = ?", (symbol,))
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to clear trailing state: {e}")
