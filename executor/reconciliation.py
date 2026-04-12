"""
executor/reconciliation.py

Сверка позиций с биржей при рестарте.
Восстановление PositionInfo из данных биржи + БД.
Проверка наличия TP и выставление emergency TP для прибыльных позиций.
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
        Проверяет наличие TP и выставляет emergency TP при необходимости.
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

                # Проверяем наличие TP и выставляем emergency TP при необходимости
                await self._check_and_restore_tp(symbol, side, avg_price, size)

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

    async def _check_and_restore_tp(self, symbol: str, side, entry_price: float, size: float):
        """
        Проверяет наличие TP у позиции.
        Если TP отсутствует и позиция в плюсе (PnL > 1.5×ATR) → ставит emergency TP на +2.0×ATR.
        Если TP отсутствует но PnL < 1.5×ATR → только логирует.
        """
        from bitget_executor import PositionSide

        try:
            # Проверяем наличие TP план-ордера
            has_tp = await self._check_tp_exists(symbol)
            if has_tp:
                return  # TP на месте, всё ок

            logger.warning(f"Reconcile: {symbol} — TP отсутствует!")

            # Получаем текущую цену
            current_price = await self.api.get_last_price(symbol)
            if not current_price or entry_price <= 0:
                logger.warning(f"Reconcile: {symbol} — не удалось получить цену для TP check")
                return

            # Оцениваем ATR как ~1.5% от цены (fallback если нет данных)
            # В реальности ATR будет из сигнала, но при рестарте его может не быть
            estimated_atr = entry_price * 0.015  # ~1.5% как proxy для ATR

            # Считаем PnL в единицах ATR
            if side == PositionSide.LONG:
                pnl_price = current_price - entry_price
            else:
                pnl_price = entry_price - current_price

            pnl_atr = pnl_price / estimated_atr if estimated_atr > 0 else 0

            if pnl_atr >= 1.5:
                # Позиция в хорошем плюсе → ставим emergency TP на +2.0×ATR
                if side == PositionSide.LONG:
                    tp_price = entry_price + estimated_atr * 2.0
                else:
                    tp_price = entry_price - estimated_atr * 2.0

                # Определяем сторону закрытия
                close_side = "sell" if side == PositionSide.LONG else "buy"

                await self.api.place_tp(
                    symbol=symbol,
                    side=close_side,
                    quantity=size,
                    tp_price=tp_price,
                )
                logger.info(
                    f"🎯 Reconcile: emergency TP placed for {symbol} @ {tp_price:.2f} "
                    f"(+2.0×ATR, PnL={pnl_atr:.1f}×ATR)"
                )
            else:
                logger.info(
                    f"Reconcile: {symbol} — TP отсутствует, PnL={pnl_atr:.1f}×ATR < 1.5×ATR, "
                    f"не ставим emergency TP (ждём сигнал)"
                )

        except Exception as e:
            logger.error(f"Reconcile: failed to check/restore TP for {symbol}: {e}")

    async def _check_tp_exists(self, symbol: str) -> bool:
        """Проверяет, есть ли активный TP план-ордер для символа."""
        try:
            from pybitget_client import OrderApi
            import asyncio
            api = OrderApi(self.api._client)
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.ordersPlanPending(
                    symbol=symbol,
                    productType=self.cfg.product_type,
                    planType="pos_profit",
                )
            )
            data = resp.get("data", {})
            orders = data.get("entrustedList", [])
            return len(orders) > 0
        except Exception as e:
            logger.warning(f"Failed to check TP for {symbol}: {e}")
            return True  # В случае ошибки — считаем что TP есть (не ставим лишний)

    async def get_sl_order(self, symbol: str) -> str:
        """Получает ID активного SL план-ордера."""
        return await self.api.get_active_sl(symbol)

    async def get_positions_from_exchange(self) -> list:
        """Прокси к API для получения позиций с биржи."""
        return await self.api.get_positions_from_exchange()
