"""
executor/reconciliation.py

Сверка позиций с биржей при рестарте.
Восстановление PositionInfo из данных биржи + БД.
"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class ReconciliationManager:
    """Сверка и восстановление позиций с биржи."""

    def __init__(self, api_client, cfg, positions: Dict, trailing_mgr=None):
        """
        Args:
            api_client: BitgetAPIClient instance
            cfg: ExecutorConfig
            positions: shared dict of PositionInfo
            trailing_mgr: ExecutorTrailingManager (для восстановления trailing state)
        """
        self.api = api_client
        self.cfg = cfg
        self._positions = positions
        self._trailing_mgr = trailing_mgr

    async def reconcile(self):
        """
        Восстанавливает позиции с биржи после рестарта.
        Сверяет с данными в памяти и БД.
        """
        if self.cfg.dry_run:
            logger.info("Reconciliation skipped (dry-run mode)")
            return

        try:
            from bitget_executor import PositionInfo, PositionSide

            exchange_positions = await self.api.get_positions_from_exchange()

            for p in exchange_positions:
                symbol = p.get("symbol", "")
                hold_side = p.get("holdSide", "long").lower()
                size = float(p.get("total", 0) or 0)
                avg_price = float(p.get("openPriceAvg", 0) or p.get("averageOpenPrice", 0) or 0)

                if size <= 0 or not symbol:
                    continue

                side = PositionSide.LONG if hold_side == "long" else PositionSide.SHORT

                # Если позиция уже в памяти — обновляем
                if symbol in self._positions:
                    pos = self._positions[symbol]
                    pos.size = size
                    pos.avg_price = avg_price
                    pos.side = side
                    logger.info(f"Reconcile: updated {symbol} {side.value} size={size}")
                else:
                    # Новая позиция — создаём
                    self._positions[symbol] = PositionInfo(
                        symbol=symbol,
                        side=side,
                        size=size,
                        avg_price=avg_price,
                        leverage=self.cfg.leverage,
                    )
                    logger.info(f"Reconcile: restored {symbol} {side.value} size={size} @ {avg_price}")

                # Восстанавливаем trailing state из БД
                if self._trailing_mgr:
                    state = await self._trailing_mgr.restore_trailing_state(symbol)
                    if state:
                        pos = self._positions[symbol]
                        pos.trailing_step = state.get("stage", 0)
                        pos.current_sl = state.get("trailing_stop", pos.current_sl)
                        logger.info(
                            f"Reconcile: restored trailing for {symbol} "
                            f"step={pos.trailing_step} sl={pos.current_sl}"
                        )

            # Проверяем: есть ли позиции в памяти, которых нет на бирже
            exchange_symbols = {p.get("symbol", "") for p in exchange_positions}
            for symbol in list(self._positions.keys()):
                if symbol not in exchange_symbols:
                    pos = self._positions[symbol]
                    if pos.is_open:
                        logger.warning(
                            f"Reconcile: {symbol} in memory but not on exchange — marking as closed"
                        )
                        from bitget_executor import PositionSide
                        pos.side = PositionSide.NONE
                        pos.size = 0

            logger.info(f"Reconciliation complete: {len(self._positions)} positions tracked")

        except Exception as e:
            logger.error(f"Reconciliation error: {e}")

    async def get_sl_order(self, symbol: str) -> str:
        """Получает ID активного SL план-ордера."""
        return await self.api.get_active_sl(symbol)

    async def get_positions_from_exchange(self) -> list:
        """Прокси к API для получения позиций с биржи."""
        return await self.api.get_positions_from_exchange()
