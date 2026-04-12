"""
executor/api_client.py

Низкоуровневые вызовы Bitget API через pybitget_client.
Все методы — async обёртки над синхронным SDK.
"""

import asyncio
import logging
import uuid
from typing import Optional, Any

import requests

logger = logging.getLogger(__name__)


class BitgetAPIClient:
    """Обёртка над pybitget_client для async-вызовов к Bitget API."""

    def __init__(self, client: Any, cfg: Any):
        """
        Args:
            client: pybitget_client.Client instance (или None для dry-run)
            cfg: ExecutorConfig
        """
        self._client = client
        self.cfg = cfg

    # ============================================================
    # Leverage
    # ============================================================

    async def set_leverage(self):
        """Устанавливает плечо для символа."""
        if not self._client:
            return
        try:
            from pybitget_client import PositionApi
            api = PositionApi(self._client)

            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.setMarginMode(
                    symbol=self.cfg.symbol,
                    productType=self.cfg.product_type,
                    marginMode=self.cfg.margin_mode,
                    marginCoin=self.cfg.margin_coin,
                )
            )

            for hold_side in ("long", "short"):
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda hs=hold_side: api.setLeverage(
                        symbol=self.cfg.symbol,
                        productType=self.cfg.product_type,
                        marginCoin=self.cfg.margin_coin,
                        leverage=str(self.cfg.leverage),
                        holdSide=hs,
                    )
                )
            logger.info(f"Leverage set to {self.cfg.leverage}x for {self.cfg.symbol}")
        except Exception as e:
            logger.warning(f"Failed to set leverage: {e}")

    # ============================================================
    # Market / Limit orders
    # ============================================================

    async def place_market_order(
        self, symbol: str, side: str, quantity: float,
        reduce_only: bool = False, client_oid: str = ""
    ) -> dict:
        """Размещает рыночный ордер."""
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        trade_side = "close" if reduce_only else "open"
        oid = client_oid or f"dina_{uuid.uuid4().hex[:12]}"

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.placeOrder(
                symbol=symbol,
                productType=self.cfg.product_type,
                marginMode=self.cfg.margin_mode,
                marginCoin=self.cfg.margin_coin,
                size=str(quantity),
                side=side,
                tradeSide=trade_side,
                orderType="market",
                clientOid=oid,
            )
        )
        return resp

    async def place_limit_order(
        self, symbol: str, side: str, quantity: float,
        price: float, reduce_only: bool = False, client_oid: str = ""
    ) -> dict:
        """Размещает лимитный ордер."""
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        trade_side = "close" if reduce_only else "open"
        oid = client_oid or f"dina_{uuid.uuid4().hex[:12]}"

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.placeOrder(
                symbol=symbol,
                productType=self.cfg.product_type,
                marginMode=self.cfg.margin_mode,
                marginCoin=self.cfg.margin_coin,
                size=str(quantity),
                price=str(price),
                side=side,
                tradeSide=trade_side,
                orderType="limit",
                clientOid=oid,
            )
        )
        return resp

    # ============================================================
    # SL / TP plan orders
    # ============================================================

    async def place_sl(self, symbol: str, side: str, quantity: float,
                       sl_price: float, client_oid: str = "") -> str:
        """Размещает стоп-лосс план-ордер. Возвращает order_id."""
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        oid = client_oid or f"sl_{uuid.uuid4().hex[:8]}"

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.placePlanOrder(
                symbol=symbol,
                productType=self.cfg.product_type,
                marginMode=self.cfg.margin_mode,
                marginCoin=self.cfg.margin_coin,
                size=str(quantity),
                triggerPrice=str(sl_price),
                side=side,
                tradeSide="close",
                triggerType="mark_price",
                orderType="market",
                planType="loss_plan",
                clientOid=oid,
            )
        )
        order_id = resp.get("data", {}).get("orderId", "")
        logger.info(f"SL placed @ {sl_price} | id={order_id}")
        return order_id

    async def place_tp(self, symbol: str, side: str, quantity: float,
                       tp_price: float, client_oid: str = "") -> str:
        """Размещает тейк-профит план-ордер. Возвращает order_id."""
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        oid = client_oid or f"tp_{uuid.uuid4().hex[:8]}"

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.placePlanOrder(
                symbol=symbol,
                productType=self.cfg.product_type,
                marginMode=self.cfg.margin_mode,
                marginCoin=self.cfg.margin_coin,
                size=str(quantity),
                triggerPrice=str(tp_price),
                side=side,
                tradeSide="close",
                triggerType="mark_price",
                orderType="market",
                planType="profit_plan",
                clientOid=oid,
            )
        )
        order_id = resp.get("data", {}).get("orderId", "")
        logger.info(f"TP placed @ {tp_price} | id={order_id}")
        return order_id

    async def cancel_plan_orders(self, symbol: str):
        """Отменяет все план-ордера для символа."""
        try:
            from pybitget_client import OrderApi
            api = OrderApi(self._client)
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.cancelAllPlanOrders(
                    symbol=symbol,
                    productType=self.cfg.product_type,
                    planType="profit_loss",
                )
            )
            logger.info(f"Plan orders cancelled for {symbol}")
        except Exception as e:
            logger.warning(f"Failed to cancel plan orders: {e}")

    async def cancel_sl_order(self, symbol: str):
        """Отменяет активный стоп-лосс план-ордер для символа."""
        try:
            from pybitget_client import OrderApi
            api = OrderApi(self._client)
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.ordersPlanPending(
                    symbol=symbol,
                    productType=self.cfg.product_type,
                    planType="pos_loss",
                )
            )
            data = resp.get("data", {})
            orders = data.get("entrustedList", [])
            for order in orders:
                order_id = order.get("orderId")
                if order_id:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda oid=order_id: api.cancelPlanOrder(
                            symbol=symbol,
                            productType=self.cfg.product_type,
                            orderId=oid,
                            planType="pos_loss",
                        )
                    )
                    logger.info(f"Cancelled SL order {order_id} for {symbol}")
        except Exception as e:
            logger.error(f"Failed to cancel SL order for {symbol}: {e}")

    async def place_emergency_sl(self, symbol: str, sl_price: float,
                                 size: float, side: str):
        """Выставляет аварийный SL."""
        try:
            from pybitget_client import OrderApi
            api = OrderApi(self._client)

            # Для long → sell, для short → buy
            trigger_side = "sell" if side.lower() == "long" else "buy"

            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.placePlanOrder(
                    symbol=symbol,
                    productType=self.cfg.product_type,
                    marginMode=self.cfg.margin_mode,
                    marginCoin=self.cfg.margin_coin,
                    size=str(size),
                    triggerPrice=str(sl_price),
                    side=trigger_side,
                    tradeSide="close",
                    triggerType="mark_price",
                    orderType="market",
                    planType="loss_plan",
                    clientOid=f"emergency_sl_{uuid.uuid4().hex[:8]}",
                )
            )
            order_id = resp.get("data", {}).get("orderId", "")
            logger.info(f"Emergency SL placed for {symbol} @ {sl_price:.2f} | id={order_id}")
        except Exception as e:
            logger.error(f"Failed to place emergency SL for {symbol}: {e}")

    # ============================================================
    # Queries
    # ============================================================

    async def get_balance(self) -> float:
        """Получает баланс аккаунта."""
        from pybitget_client import AccountApi
        api = AccountApi(self._client)
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.accounts(productType=self.cfg.product_type)
        )
        data = resp.get("data", [])
        if data:
            return float(data[0].get("usdtEquity", 0) or data[0].get("available", 0))
        return 0.0

    async def get_funding_rate(self, symbol: str) -> float:
        """Получает текущий funding rate."""
        try:
            url = f"https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol={symbol}&productType=USDT-FUTURES"
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: requests.get(url, timeout=5)
            )
            data = resp.json()
            if data.get("code") == "00000" and data.get("data"):
                return float(data["data"][0].get("fundingRate", 0))
        except Exception as e:
            logger.error(f"Failed to get funding rate: {e}")
        return 0.0

    async def get_last_price(self, symbol: str) -> Optional[float]:
        """Получает последнюю цену."""
        try:
            url = f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES"
            resp = requests.get(url, timeout=5)
            data = resp.json()
            if data.get("code") == "00000" and data.get("data"):
                return float(data["data"][0]["lastPr"])
            return None
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return None

    async def get_positions_from_exchange(self) -> list:
        """Получает все открытые позиции с биржи."""
        if not self._client:
            return []
        try:
            from pybitget_client import PositionApi
            api = PositionApi(self._client)
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.allPosition(
                    productType=self.cfg.product_type,
                    marginCoin=self.cfg.margin_coin,
                )
            )
            positions = []
            for p in resp.get("data", []):
                size = float(p.get("total", 0) or 0)
                if size > 0:
                    positions.append(p)
            return positions
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    async def wait_fill(self, order_id: str, symbol: str, timeout: float = 5.0) -> tuple:
        """
        Ожидает исполнения ордера.
        Returns: (filled_price, commission) — tuple(float|None, float)
        """
        import time
        from pybitget_client import OrderApi
        deadline = time.time() + timeout
        api = OrderApi(self._client)

        while time.time() < deadline:
            try:
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: api.detail(
                        symbol=symbol,
                        productType=self.cfg.product_type,
                        orderId=order_id,
                    )
                )
                data = resp.get("data", {})
                status = data.get("status", "")
                if status == "filled":
                    price = float(data.get("priceAvg", 0) or 0)
                    # Извлекаем комиссию из ответа биржи
                    fee = abs(float(data.get("fee", 0) or data.get("commission", 0) or 0))
                    logger.info(f"Order {order_id} filled @ {price}, fee={fee}")
                    return (price if price else None, fee)
                if status in ("cancelled", "failed"):
                    return (None, 0.0)
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return (None, 0.0)

    async def get_active_sl(self, symbol: str) -> str:
        """Получает ID активного SL план-ордера."""
        try:
            from pybitget_client import OrderApi
            api = OrderApi(self._client)
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.ordersPlanPending(
                    symbol=symbol,
                    productType=self.cfg.product_type,
                    planType="pos_loss",
                )
            )
            data = resp.get("data", {})
            orders = data.get("entrustedList", [])
            if orders:
                return orders[0].get("orderId", "")
        except Exception as e:
            logger.warning(f"Failed to get active SL: {e}")
        return ""
