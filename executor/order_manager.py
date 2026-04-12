"""
executor/order_manager.py

Открытие/закрытие позиций, расчёт количества, логирование ордеров.
"""

import asyncio
import logging
import time
import uuid
from typing import Optional, Dict, Any

import aiosqlite

logger = logging.getLogger(__name__)


class OrderManager:
    """Управление ордерами: open, close, partial_close, logging."""

    def __init__(self, api_client, cfg, positions: Dict, db_path: str):
        """
        Args:
            api_client: BitgetAPIClient instance
            cfg: ExecutorConfig
            positions: shared dict of PositionInfo (symbol -> PositionInfo)
            db_path: path to SQLite DB
        """
        self.api = api_client
        self.cfg = cfg
        self._positions = positions
        self._db_path = db_path

    def calc_quantity(self, size_usd: float, price: float) -> float:
        """Рассчитывает количество контрактов."""
        if price <= 0:
            return 0.0
        return round(size_usd / price, 6)

    async def open_position(self, req) -> Any:
        """
        Открывает позицию: entry order + SL + TP.
        
        Args:
            req: OrderRequest
        Returns:
            OrderResult
        """
        from bitget_executor import OrderResult, PositionInfo, PositionSide, OrderSide

        symbol = req.symbol or self.cfg.symbol
        quantity = self.calc_quantity(req.size_usd, req.entry_price)

        if quantity <= 0:
            return OrderResult(success=False, error="quantity <= 0")

        # Dry-run mode
        if self.cfg.dry_run:
            trade_id = f"dry_{uuid.uuid4().hex[:8]}"
            result = OrderResult(
                success=True,
                order_id=f"dry_{uuid.uuid4().hex[:8]}",
                client_oid=req.client_oid,
                filled_price=req.entry_price,
                filled_size=quantity,
                sl_order_id=f"dry_sl_{uuid.uuid4().hex[:6]}",
                tp_order_id=f"dry_tp_{uuid.uuid4().hex[:6]}",
                dry_run=True,
                trade_id=trade_id,
            )
            # Сохраняем позицию в памяти
            side = PositionSide.LONG if req.direction.lower() == "long" else PositionSide.SHORT
            self._positions[symbol] = PositionInfo(
                symbol=symbol,
                side=side,
                size=quantity,
                avg_price=req.entry_price,
                leverage=self.cfg.leverage,
                trade_id=trade_id,
                initial_sl=req.sl_price,
                current_sl=req.sl_price,
            )
            await self._log_order(req, result, "open")
            return result

        # Real mode
        try:
            entry_side = OrderSide.BUY if req.direction.lower() == "long" else OrderSide.SELL
            close_side = OrderSide.SELL if req.direction.lower() == "long" else OrderSide.BUY

            # 1. Entry order
            resp = await self.api.place_market_order(
                symbol=symbol,
                side=entry_side.value,
                quantity=quantity,
                client_oid=req.client_oid,
            )
            order_id = resp.get("data", {}).get("orderId", "")
            if not order_id:
                return OrderResult(success=False, error=f"No order_id: {resp}")

            # 2. Wait fill
            filled_price = await self.api.wait_fill(order_id, symbol)
            if not filled_price:
                filled_price = req.entry_price

            # 3. SL
            sl_id = await self.api.place_sl(
                symbol=symbol,
                side=close_side.value,
                quantity=quantity,
                sl_price=req.sl_price,
            )

            # 4. TP
            tp_id = await self.api.place_tp(
                symbol=symbol,
                side=close_side.value,
                quantity=quantity,
                tp_price=req.tp_price,
            )

            trade_id = f"trade_{uuid.uuid4().hex[:8]}"
            result = OrderResult(
                success=True,
                order_id=order_id,
                client_oid=req.client_oid,
                filled_price=filled_price,
                filled_size=quantity,
                sl_order_id=sl_id,
                tp_order_id=tp_id,
                trade_id=trade_id,
            )

            # Save position
            side = PositionSide.LONG if req.direction.lower() == "long" else PositionSide.SHORT
            self._positions[symbol] = PositionInfo(
                symbol=symbol,
                side=side,
                size=quantity,
                avg_price=filled_price,
                leverage=self.cfg.leverage,
                trade_id=trade_id,
                initial_sl=req.sl_price,
                current_sl=req.sl_price,
            )

            await self._log_order(req, result, "open")
            return result

        except Exception as e:
            logger.error(f"open_position error: {e}")
            return OrderResult(success=False, error=str(e))

    async def close_position(self, symbol: str, reason: str = "signal") -> Any:
        """Закрывает позицию полностью."""
        from bitget_executor import OrderResult, OrderSide, PositionSide

        pos = self._positions.get(symbol)
        if not pos or not pos.is_open:
            return OrderResult(success=False, error=f"No open position for {symbol}")

        if self.cfg.dry_run:
            result = OrderResult(
                success=True,
                order_id=f"dry_close_{uuid.uuid4().hex[:8]}",
                filled_price=pos.avg_price,
                filled_size=pos.size,
                dry_run=True,
                trade_id=pos.trade_id,
            )
            pos.side = PositionSide.NONE
            pos.size = 0
            await self._log_order_close(result, reason)
            return result

        try:
            # Cancel existing plan orders
            await self.api.cancel_plan_orders(symbol)

            close_side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
            resp = await self.api.place_market_order(
                symbol=symbol,
                side=close_side.value,
                quantity=pos.size,
                reduce_only=True,
            )
            order_id = resp.get("data", {}).get("orderId", "")
            filled_price = await self.api.wait_fill(order_id, symbol) if order_id else pos.avg_price

            result = OrderResult(
                success=True,
                order_id=order_id,
                filled_price=filled_price or pos.avg_price,
                filled_size=pos.size,
                trade_id=pos.trade_id,
            )
            pos.side = PositionSide.NONE
            pos.size = 0
            await self._log_order_close(result, reason)
            return result

        except Exception as e:
            logger.error(f"close_position error: {e}")
            return OrderResult(success=False, error=str(e))

    async def partial_close(self, symbol: str, side: str, pct: float):
        """Закрыть pct% позиции по рынку."""
        pos = self._positions.get(symbol)
        if not pos:
            return
        close_size = round(pos.size * pct, 6)

        if self.cfg.dry_run:
            logger.info(f"[DRY] partial_close {symbol} {pct*100:.0f}% size={close_size}")
            pos.size = round(pos.size - close_size, 6)
            return

        from bitget_executor import OrderSide
        close_side = OrderSide.SELL if side.lower() == "long" else OrderSide.BUY
        await self.api.place_market_order(
            symbol=symbol,
            side=close_side.value,
            quantity=close_size,
            reduce_only=True,
        )
        pos.size = round(pos.size - close_size, 6)

    # ============================================================
    # DB logging
    # ============================================================

    async def _log_order(self, req, result, action: str):
        """Логирует ордер в БД."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """INSERT INTO order_log (ts, client_oid, order_id, action, direction,
                       size_usd, entry_price, sl_price, tp_price, filled_price, error, dry_run, trade_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), req.client_oid, result.order_id, action, req.direction,
                     req.size_usd, req.entry_price, req.sl_price, req.tp_price,
                     result.filled_price, result.error, result.dry_run, result.trade_id)
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to log order: {e}")

    async def _log_order_close(self, result, reason: str):
        """Логирует закрытие ордера в БД."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """INSERT INTO order_log (ts, client_oid, order_id, action, direction,
                       size_usd, entry_price, sl_price, tp_price, filled_price, error, dry_run, trade_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), "", result.order_id, f"close_{reason}", "",
                     0, 0, 0, 0, result.filled_price, result.error, result.dry_run, result.trade_id)
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to log close: {e}")
