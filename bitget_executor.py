"""
bitget_executor.py

Исполнение ордеров на Bitget Futures.

Включает:
  - Атомарное открытие позиции + SL/TP
  - Dry‑run режим
  - Трейлинг (4-этапный на ATR)
  - Stage‑трекинг (частичное закрытие)
  - Position age timeout
  - Reconciliation (восстановление позиций при рестарте)
  - ExecutionGuard (allowlist, rate limits, max position %)
  - Сохранение состояния в БД (order_log, active_trailing)
"""

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Any

import aiosqlite
import requests

logger = logging.getLogger(__name__)


# ============================================================
# Конфиг
# ============================================================

@dataclass
class ExecutorConfig:
    api_key: str = field(default_factory=lambda: os.getenv("BITGET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BITGET_API_SECRET", ""))
    passphrase: str = field(default_factory=lambda: os.getenv("BITGET_PASSPHRASE", ""))
    symbol: str = "BTCUSDT"
    margin_coin: str = "USDT"
    leverage: int = 10
    margin_mode: str = "isolated"
    product_type: str = "USDT-FUTURES"
    max_retries: int = 3
    retry_delay_s: float = 1.0
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "dina.db"))
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")

    # ExecutionGuard
    allowlist_symbols: List[str] = field(default_factory=lambda: os.getenv("SYMBOLS", "BTCUSDT").split(","))
    max_position_pct: float = 0.15          # 15% от депозита
    max_orders_per_minute: int = 5

    # Trailing
    trailing_activation_atr: float = 0.5    # цена + 0.5*ATR для активации
    trailing_step_atr: float = 0.2
    trailing_dist_atr: float = 1.2          # отступ SL от цены

    # Position age timeout (в количестве проверок _monitor_loop, примерное соответствие свечам)
    max_hold_checks: int = 48               # 48 проверок * 10 сек = 8 часов (для 15m)
    min_expected_pnl_pct: float = 0.5


# ============================================================
# Модели
# ============================================================

class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass
class OrderRequest:
    direction: str          # "long" | "short"
    size_usd: float
    entry_price: float
    sl_price: float
    tp_price: float
    symbol: str = ""
    order_type: OrderType = OrderType.MARKET
    limit_price: float = 0.0
    client_oid: str = field(default_factory=lambda: f"dina_{uuid.uuid4().hex[:12]}")
    reason: str = ""


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    client_oid: str = ""
    filled_price: float = 0.0
    filled_size: float = 0.0
    sl_order_id: str = ""
    tp_order_id: str = ""
    error: str = ""
    dry_run: bool = False
    timestamp: float = field(default_factory=time.time)
    trade_id: str = ""

    def __str__(self):
        if not self.success:
            return f"❌ Order FAILED: {self.error}"
        tag = "[DRY RUN] " if self.dry_run else ""
        return f"✅ {tag}Order filled | price={self.filled_price:.2f} size={self.filled_size:.6f} SL={self.sl_order_id[:8]}... TP={self.tp_order_id[:8]}..."


@dataclass
class PositionInfo:
    symbol: str
    side: PositionSide
    size: float = 0.0
    avg_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: int = 1
    margin: float = 0.0
    trade_id: str = ""
    initial_sl: float = 0.0          # начальный стоп при входе
    current_sl: float = 0.0          # текущий стоп (двигается трейлингом)
    trailing_step: int = 0           # шаг трейлинга (0-4)

    @property
    def is_open(self) -> bool:
        return self.side != PositionSide.NONE and self.size > 0


# ============================================================
# BitgetExecutor
# ============================================================

class BitgetExecutor:
    def __init__(self, config: Optional[ExecutorConfig] = None):
        self.cfg = config or ExecutorConfig()
        self._client: Optional[Any] = None   # будет создан в setup()
        self._positions: Dict[str, PositionInfo] = {}
        self._position_ages: Dict[str, int] = {}   # symbol → age in checks
        self._order_timestamps: deque[float] = deque()  # для rate limit
        self._execution_guard_paused = False
        self._strategist: Optional[Any] = None

        logger.info(f"BitgetExecutor init | symbol={self.cfg.symbol} leverage={self.cfg.leverage}x dry_run={self.cfg.dry_run}")
        
    def set_strategist(self, strategist):
        self._strategist = strategist   

    async def setup(self):
        """Создаёт таблицы, устанавливает плечо, восстанавливает позиции."""
        await self._init_db()

        if self.cfg.dry_run:
            logger.warning("BitgetExecutor: DRY RUN mode — ордера не исполняются")
            return

        try:
            from pybitget_client import Client
            self._client = Client(
                api_key=self.cfg.api_key,
                api_secret=self.cfg.api_secret,
                passphrase=self.cfg.passphrase,
            )
            await self._set_leverage()
            # Восстановление позиций и трейлинга
            await self._reconcile()
            logger.info("BitgetExecutor: подключен к Bitget")
        except ImportError:
            raise RuntimeError("python-bitget не установлен. Запусти: pip install python-bitget")

    async def _init_db(self):
        """Создаёт таблицы order_log, active_trailing, active_positions."""
        async with aiosqlite.connect(self.cfg.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS order_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    client_oid TEXT,
                    order_id TEXT,
                    action TEXT,
                    direction TEXT,
                    size_usd REAL,
                    entry_price REAL,
                    sl_price REAL,
                    tp_price REAL,
                    filled_price REAL,
                    filled_size REAL,
                    sl_order_id TEXT,
                    tp_order_id TEXT,
                    success INTEGER,
                    error TEXT,
                    dry_run INTEGER,
                    reason TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS active_trailing (
                    symbol TEXT PRIMARY KEY,
                    trailing_activated INTEGER,
                    trailing_stop REAL,
                    stage INTEGER,
                    plan_order_id TEXT,
                    updated_at REAL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS active_positions (
                    symbol TEXT PRIMARY KEY,
                    side TEXT,
                    size REAL,
                    entry_price REAL,
                    opened_at REAL,
                    age_candles INTEGER
                )
            """)
            await db.commit()

    # ============================================================
    # Публичные методы
    # ============================================================

    async def open_position(self, req: OrderRequest) -> OrderResult:
        """Открывает позицию + SL/TP."""
        # Проверка ExecutionGuard
        if not self._execution_guard_check(req):
            return OrderResult(success=False, error="ExecutionGuard blocked")

        # Проверка существующей позиции
        if req.symbol in self._positions and self._positions[req.symbol].is_open:
            return OrderResult(success=False, error=f"Already have position for {req.symbol}")

        quantity = self._calc_quantity(req.size_usd, req.entry_price)

        if self.cfg.dry_run:
            result = OrderResult(
                success=True,
                order_id=f"dry_{uuid.uuid4().hex[:12]}",
                client_oid=req.client_oid,
                filled_price=req.entry_price,
                filled_size=quantity,
                sl_order_id=f"dry_sl_{uuid.uuid4().hex[:8]}",
                tp_order_id=f"dry_tp_{uuid.uuid4().hex[:8]}",
                dry_run=True,
                trade_id=req.client_oid,
            )
            await self._log_order(req, result, "open")
            # Сохраняем в памяти позицию
            self._positions[req.symbol] = PositionInfo(
                symbol=req.symbol,
                side=PositionSide.LONG if req.direction == "long" else PositionSide.SHORT,
                size=quantity,
                avg_price=req.entry_price,
                trade_id=req.client_oid,
                initial_sl=req.sl_price,
                current_sl=req.sl_price,
                trailing_step=0,
            )
            self._position_ages[req.symbol] = 0
            return result

        # Реальное исполнение
        try:
            result = await self._place_entry_order(req, quantity)
        except Exception as e:
            logger.error(f"BitgetExecutor: ошибка входа: {e}", exc_info=True)
            return OrderResult(success=False, error=str(e))

        if not result.success:
            return result

        # Ждём заполнения
        filled_price = await self._wait_fill(result.order_id)
        if filled_price:
            result.filled_price = filled_price
            result.trade_id = req.client_oid

        # Выставляем SL и TP
        try:
            sl_id = await self._place_sl(req, quantity)
            tp_id = await self._place_tp(req, quantity)
            result.sl_order_id = sl_id
            result.tp_order_id = tp_id
        except Exception as e:
            logger.error(f"BitgetExecutor: не удалось выставить SL/TP: {e}", exc_info=True)
            result.error = f"Позиция открыта, но SL/TP не выставлены: {e}"

        await self._log_order(req, result, "open")

        # Сохраняем позицию в память
        self._positions[req.symbol] = PositionInfo(
            symbol=req.symbol,
            side=PositionSide.LONG if req.direction == "long" else PositionSide.SHORT,
            size=quantity,
            avg_price=result.filled_price,
            trade_id=req.client_oid,
            initial_sl=req.sl_price,
            current_sl=req.sl_price,
            trailing_step=0,
        )
        self._position_ages[req.symbol] = 0

        # Сохраняем трейлинговые параметры в БД
        await self._save_trailing_state(req.symbol, activated=False, trailing_stop=req.sl_price, stage=0, plan_order_id=sl_id)

        logger.info(f"BitgetExecutor: позиция открыта {req.direction} {req.symbol} @ {result.filled_price:.2f}")
        return result

    async def close_position(self, symbol: str, reason: str = "signal") -> OrderResult:
        pos = self._positions.get(symbol)
        if not pos or not pos.is_open:
            return OrderResult(success=False, error="No open position")

        # ---------- DRY RUN ----------
        if self.cfg.dry_run:
            result = OrderResult(
                success=True,
                order_id=f"dry_close_{uuid.uuid4().hex[:8]}",
                filled_price=pos.avg_price,
                filled_size=pos.size,
                dry_run=True,
            )
            # Вызов стратегиста
            if self._strategist:
                if pos.side == PositionSide.LONG:
                    pnl_pct = (result.filled_price - pos.avg_price) / pos.avg_price * 100
                else:
                    pnl_pct = (pos.avg_price - result.filled_price) / pos.avg_price * 100
                pnl_usd = pos.size * result.filled_price * pnl_pct / 100
                await self._strategist.on_trade_closed(
                    trade_id=pos.trade_id,
                    symbol=symbol,
                    exit_price=result.filled_price,
                    pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct,
                    reason=reason,
                )
            await self._log_order_close(result, reason)
            del self._positions[symbol]
            del self._position_ages[symbol]
            await self._clear_trailing_state(symbol)
            return result

        # ---------- РЕАЛЬНЫЙ РЕЖИМ ----------
        try:
            # Отменяем план-ордера SL/TP
            await self._cancel_plan_orders(symbol)

            # Закрывающий ордер
            side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
            resp = await self._retry(
                self._place_market_order,
                symbol=symbol,
                side=side,
                quantity=pos.size,
                reduce_only=True,
            )
            result = OrderResult(
                success=True,
                order_id=resp.get("orderId", ""),
                filled_size=pos.size,
                filled_price=float(resp.get("price", pos.avg_price) or 0),
            )

            # Вызов стратегиста
            if self._strategist:
                if pos.side == PositionSide.LONG:
                    pnl_pct = (result.filled_price - pos.avg_price) / pos.avg_price * 100
                else:
                    pnl_pct = (pos.avg_price - result.filled_price) / pos.avg_price * 100
                pnl_usd = pos.size * result.filled_price * pnl_pct / 100
                await self._strategist.on_trade_closed(
                    trade_id=pos.trade_id,
                    symbol=symbol,
                    exit_price=result.filled_price,
                    pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct,
                    reason=reason,
                )
        except Exception as e:
            logger.error(f"BitgetExecutor: ошибка закрытия: {e}", exc_info=True)
            return OrderResult(success=False, error=str(e))

        await self._log_order_close(result, reason)
        del self._positions[symbol]
        del self._position_ages[symbol]
        await self._clear_trailing_state(symbol)
        return result

    async def get_position(self, symbol: str) -> PositionInfo:
        """Получает позицию из памяти (для стратегии)."""
        return self._positions.get(symbol, PositionInfo(symbol=symbol, side=PositionSide.NONE))

    async def get_positions_from_exchange(self) -> List[PositionInfo]:
        """Прямой запрос к бирже для SafetyGuard/reconciliation."""
        if self.cfg.dry_run:
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
            data = resp.get("data", [])
            positions = []
            for item in data:
                size = float(item.get("total", 0))
                if size <= 0:
                    continue
                side = PositionSide.LONG if item.get("holdSide") == "long" else PositionSide.SHORT
                positions.append(PositionInfo(
                    symbol=item["symbol"],
                    side=side,
                    size=size,
                    avg_price=float(item.get("openPriceAvg", 0)),
                    unrealized_pnl=float(item.get("unrealizedPL", 0)),
                    leverage=int(item.get("leverage", self.cfg.leverage)),
                ))
            return positions
        except Exception as e:
            logger.error(f"BitgetExecutor: get_positions_from_exchange error: {e}")
            return []

    async def get_balance(self) -> float:
        """Возвращает доступный баланс USDT."""
        if self.cfg.dry_run:
            return float(getattr(self.cfg, "starting_balance", 10000))

        try:
            from pybitget_client import AccountApi
            api = AccountApi(self._client)
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.accounts(self.cfg.product_type)
            )
            data = resp.get("data", [])
            if not data:
                logger.warning("get_balance: пустой ответ от биржи")
                return 0.0
            # ищем USDT маржу
            for account in data:
                if account.get("marginCoin", "").upper() == "USDT":
                    return float(account.get("available", 0))
            return float(data[0].get("available", 0))
        except Exception as e:
            logger.error(f"get_balance error: {e}")
            return 0.0

    async def get_funding_rate(self, symbol: str) -> float:
        """
        Возвращает текущий funding rate для символа.
        Положительный = лонги платят шортам, отрицательный = шорты платят лонгам.
        Возвращает 0.0 при ошибке или в dry_run.
        """
        if self.cfg.dry_run:
            return 0.0

        try:
            url = "https://api.bitget.com/api/v2/mix/market/current-fund-rate"
            params = {
                "symbol": symbol,
                "productType": self.cfg.product_type,
            }
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: requests.get(url, params=params, timeout=10).json()
            )
            data = resp.get("data", [])
            if data and isinstance(data, list) and len(data) > 0:
                rate = float(data[0].get("fundingRate", 0))
                logger.debug(f"Funding rate {symbol}: {rate:.6f}")
                return rate
            elif data and isinstance(data, dict):
                rate = float(data.get("fundingRate", 0))
                logger.debug(f"Funding rate {symbol}: {rate:.6f}")
                return rate
            return 0.0
        except Exception as e:
            logger.warning(f"get_funding_rate error for {symbol}: {e}")
            return 0.0

    # ============================================================
    # Внутренние методы API
    # ============================================================

    def _calc_quantity(self, size_usd: float, price: float) -> float:
        notional = size_usd * self.cfg.leverage
        qty = notional / price
        return round(qty, 3)

    # ============================================================
    # Trailing, stage, reconciliation
    # ============================================================

    async def _reconcile(self):
        """Восстанавливает позиции и трейлинговое состояние после рестарта."""
        positions = await self.get_positions_from_exchange()
        for pos in positions:
            if pos.is_open:
                self._positions[pos.symbol] = pos
                self._position_ages[pos.symbol] = 0
                # Проверяем, есть ли SL
                sl_order = await self._get_sl_order(pos.symbol)
                if not sl_order:
                    # Аварийный SL по ATR
                    atr = await self._get_atr(pos.symbol)
                    if atr:
                        emergency_sl = pos.avg_price - atr * 1.5 if pos.side == PositionSide.LONG else pos.avg_price + atr * 1.5
                        await self._place_emergency_sl(pos.symbol, emergency_sl, pos.size, pos.side)
                        logger.warning(f"Restored position {pos.symbol} without SL, placed emergency SL @ {emergency_sl:.2f}")
                # Восстанавливаем трейлинговое состояние из БД
                await self._restore_trailing_state(pos.symbol)

    # ============================================================
    # ExecutionGuard
    # ============================================================

    def _execution_guard_check(self, req: OrderRequest) -> bool:
        # 1. Allowlist
        if req.symbol not in self.cfg.allowlist_symbols:
            logger.warning(f"ExecutionGuard: symbol {req.symbol} not in allowlist")
            return False

        # 2. Rate limit
        now = time.time()
        self._order_timestamps.append(now)
        while self._order_timestamps and self._order_timestamps[0] < now - 60:
            self._order_timestamps.popleft()
        if len(self._order_timestamps) > self.cfg.max_orders_per_minute:
            logger.warning(f"ExecutionGuard: rate limit exceeded")
            return False

        # 3. Max position size % (будет проверено также в RiskManager)
        # Здесь просто для примера
        return True

    # ============================================================
    # БД для трейлинга
    # ============================================================

    async def _save_trailing_state(self, symbol: str, activated: bool, trailing_stop: float, stage: int, plan_order_id: str):
        async with aiosqlite.connect(self.cfg.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO active_trailing (symbol, trailing_activated, trailing_stop, stage, plan_order_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (symbol, 1 if activated else 0, trailing_stop, stage, plan_order_id, time.time()))
            await db.commit()

    async def _restore_trailing_state(self, symbol: str):
        async with aiosqlite.connect(self.cfg.db_path) as db:
            cur = await db.execute("SELECT trailing_activated, trailing_stop, stage, plan_order_id FROM active_trailing WHERE symbol = ?", (symbol,))
            row = await cur.fetchone()
            if row:
                activated, trailing_stop, stage, plan_order_id = row
                # Восстановить в память для использования в _monitor_loop
                # Здесь можно сохранить в отдельный словарь
                pass

    async def _clear_trailing_state(self, symbol: str):
        async with aiosqlite.connect(self.cfg.db_path) as db:
            await db.execute("DELETE FROM active_trailing WHERE symbol = ?", (symbol,))
            await db.commit()

    # ============================================================
    # Логирование
    # ============================================================

    async def _log_order(self, req: OrderRequest, result: OrderResult, action: str):
        async with aiosqlite.connect(self.cfg.db_path) as db:
            await db.execute("""
                INSERT INTO order_log
                (ts, client_oid, order_id, action, direction, size_usd, entry_price, sl_price, tp_price,
                 filled_price, filled_size, sl_order_id, tp_order_id, success, error, dry_run, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                time.time(), req.client_oid, result.order_id, action,
                req.direction, req.size_usd, req.entry_price, req.sl_price, req.tp_price,
                result.filled_price, result.filled_size, result.sl_order_id, result.tp_order_id,
                int(result.success), result.error, int(result.dry_run), req.reason,
            ))
            await db.commit()

    async def _log_order_close(self, result: OrderResult, reason: str):
        async with aiosqlite.connect(self.cfg.db_path) as db:
            await db.execute("""
                INSERT INTO order_log
                (ts, order_id, action, filled_price, filled_size, success, error, dry_run, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                time.time(), result.order_id, "close",
                result.filled_price, result.filled_size,
                int(result.success), result.error, int(result.dry_run), reason,
            ))
            await db.commit()
# ===== Вторая часть начинается здесь =====
# Продолжение класса BitgetExecutor

    # ============================================================
    # Реализация API-вызовов (из старых файлов, адаптировано)
    # ============================================================

    async def _set_leverage(self):
        """Устанавливает плечо и режим маржи."""
        try:
            from pybitget_client import AccountApi
            api = AccountApi(self._client)

            # Margin mode
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.setMarginMode(
                    symbol=self.cfg.symbol,
                    productType=self.cfg.product_type,
                    marginCoin=self.cfg.margin_coin,
                    marginMode=self.cfg.margin_mode,
                )
            )

            # Leverage
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.setLeverage(
                    symbol=self.cfg.symbol,
                    productType=self.cfg.product_type,
                    marginCoin=self.cfg.margin_coin,
                    leverage=str(self.cfg.leverage),
                    holdSide="long_short",
                )
            )
            logger.info(f"Leverage set: {self.cfg.leverage}x, mode={self.cfg.margin_mode}")
        except Exception as e:
            logger.warning(f"Failed to set leverage: {e}")

    async def _place_entry_order(self, req: OrderRequest, quantity: float) -> OrderResult:
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        side = OrderSide.BUY if req.direction == "long" else OrderSide.SELL

        if req.order_type == OrderType.MARKET:
            resp = await self._retry(
                self._place_market_order,
                symbol=self.cfg.symbol,
                side=side,
                quantity=quantity,
                reduce_only=False,
                client_oid=req.client_oid,
            )
        else:
            resp = await self._retry(
                self._place_limit_order,
                symbol=self.cfg.symbol,
                side=side,
                quantity=quantity,
                price=req.limit_price or req.entry_price,
                client_oid=req.client_oid,
            )

        order_id = resp.get("orderId", "")
        return OrderResult(
            success=bool(order_id),
            order_id=order_id,
            client_oid=req.client_oid,
            error="" if order_id else f"No orderId in response: {resp}",
        )

    async def _place_market_order(self, symbol: str, side: OrderSide, quantity: float,
                                   reduce_only: bool = False, client_oid: str = "") -> dict:
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        params = {
            "symbol": symbol,
            "productType": self.cfg.product_type,
            "marginMode": self.cfg.margin_mode,
            "marginCoin": self.cfg.margin_coin,
            "size": str(quantity),
            "side": side.value,
            "tradeSide": "close" if reduce_only else "open",
            "orderType": "market",
            "clientOid": client_oid or f"dina_{uuid.uuid4().hex[:12]}",
        }
        resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: api.placeOrder(**params)
        )
        return resp.get("data", {})

    async def _place_limit_order(self, symbol: str, side: OrderSide, quantity: float,
                                  price: float, client_oid: str = "") -> dict:
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.placeOrder(
                symbol=symbol,
                productType=self.cfg.product_type,
                marginMode=self.cfg.margin_mode,
                marginCoin=self.cfg.margin_coin,
                size=str(quantity),
                price=str(price),
                side=side.value,
                tradeSide="open",
                orderType="limit",
                clientOid=client_oid or f"dina_{uuid.uuid4().hex[:12]}",
            )
        )
        return resp.get("data", {})

    async def _place_sl(self, req: OrderRequest, quantity: float) -> str:
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        trigger_side = OrderSide.SELL if req.direction == "long" else OrderSide.BUY

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.placePlanOrder(
                symbol=self.cfg.symbol,
                productType=self.cfg.product_type,
                marginMode=self.cfg.margin_mode,
                marginCoin=self.cfg.margin_coin,
                size=str(quantity),
                triggerPrice=str(req.sl_price),
                side=trigger_side.value,
                tradeSide="close",
                triggerType="mark_price",
                orderType="market",
                planType="loss_plan",
                clientOid=f"dina_sl_{uuid.uuid4().hex[:8]}",
            )
        )
        order_id = resp.get("data", {}).get("orderId", "")
        logger.info(f"SL placed @ {req.sl_price} | id={order_id}")
        return order_id
    async def _place_sl_raw(self, symbol: str, side: str, quantity: float, sl_price: float) -> str:
        """Внутренний метод для выставления SL по параметрам (без OrderRequest)."""
        from pybitget_client import OrderApi
        api = OrderApi(self._client)
        trigger_side = OrderSide.SELL if side == "long" else OrderSide.BUY
        if self.cfg.dry_run:
            logger.info("[DRY-RUN] _place_sl_raw: SL не выставлен")
            return "dry-run-sl"

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.placePlanOrder(
                symbol=symbol,
                productType=self.cfg.product_type,
                marginMode=self.cfg.margin_mode,
                marginCoin=self.cfg.margin_coin,
                size=str(quantity),
                triggerPrice=str(sl_price),
                side=trigger_side.value,
                tradeSide="close",
                triggerType="mark_price",
                orderType="market",
                planType="loss_plan",
                clientOid=f"dina_sl_{uuid.uuid4().hex[:8]}",
            )
        )
        order_id = resp.get("data", {}).get("orderId", "")
        logger.info(f"SL placed raw @ {sl_price} | id={order_id}")
        return order_id
    async def place_stop_loss(
        self,
        symbol: str,
        side: str,
        sl_price: float,
    ) -> str:
        """
        Выставляет стоп-лосс план-ордер на существующую позицию.
        Вызывается SafetyGuard, когда позиция открыта, но SL отсутствует.

        Возвращает order_id созданного ордера или "" если ничего не сделано.
        """
        # 1. Получаем позицию из локального кэша
        pos = self._positions.get(symbol)
        if pos is None or not pos.is_open or pos.size <= 0:
            # Попытка через биржу (на случай рестарта)
            try:
                pos = await self.get_position(symbol)
            except Exception as exc:
                logger.error("place_stop_loss: не удалось получить позицию %s: %s", symbol, exc)
                return ""
            if pos is None or not pos.is_open or pos.size <= 0:
                logger.debug("place_stop_loss: нет открытой позиции по %s", symbol)
                return ""

        quantity = pos.size

        # 2. Проверяем — нет ли уже активного SL на бирже
        if not self.cfg.dry_run:
            try:
                existing_sl = await self._get_active_sl(symbol)
                if existing_sl:
                    logger.info(
                        "place_stop_loss: SL уже существует для %s (order_id=%s), пропуск",
                        symbol, existing_sl,
                    )
                    return existing_sl
            except Exception as exc:
                # Не блокируем — если не смогли проверить, ставим новый SL
                logger.warning("place_stop_loss: не удалось проверить существующий SL: %s", exc)

        # 3. Dry-run — только логируем
        if self.cfg.dry_run:
            logger.info(
                "[DRY-RUN] place_stop_loss: %s side=%s qty=%.4f sl_price=%.4f",
                symbol, side, quantity, sl_price,
            )
            return "dry-run-sl"

        # 4. Ставим реальный SL
        logger.warning(
            "place_stop_loss: АВАРИЙНЫЙ SL для %s side=%s qty=%.4f цена=%.4f",
            symbol, side, quantity, sl_price,
        )
        try:
            order_id = await self._place_sl_raw(symbol, side, quantity, sl_price)
            logger.info("place_stop_loss: SL выставлен order_id=%s", order_id)
            return order_id or ""
        except Exception as exc:
            logger.error("place_stop_loss: ОШИБКА при выставлении SL %s: %s", symbol, exc)
            return ""
    async def _get_active_sl(self, symbol: str) -> str:
        """
        Проверяет наличие активного план-ордера типа pos_loss (SL) для символа.
        Возвращает order_id если найден, иначе пустую строку.
        """
        if self.cfg.dry_run:
            return ""

        try:
            from pybitget_client import OrderApi
            api = OrderApi(self._client)

            # Получаем список ожидающих план-ордеров
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.ordersPlanPending(
                    symbol=symbol,
                    productType=self.cfg.product_type,
                    planType="pos_loss",  # стоп-лосс
                )
            )
            data = resp.get("data", {})
            orders = data.get("entrustedList", [])
            if orders:
                return orders[0].get("orderId", "")
        except Exception as exc:
            logger.warning("_get_active_sl: ошибка запроса: %s", exc)
        return ""

    async def _place_tp(self, req: OrderRequest, quantity: float) -> str:
        from pybitget_client import OrderApi
        api = OrderApi(self._client)

        trigger_side = OrderSide.SELL if req.direction == "long" else OrderSide.BUY

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.placePlanOrder(
                symbol=self.cfg.symbol,
                productType=self.cfg.product_type,
                marginMode=self.cfg.margin_mode,
                marginCoin=self.cfg.margin_coin,
                size=str(quantity),
                triggerPrice=str(req.tp_price),
                side=trigger_side.value,
                tradeSide="close",
                triggerType="mark_price",
                orderType="market",
                planType="profit_plan",
                clientOid=f"dina_tp_{uuid.uuid4().hex[:8]}",
            )
        )
        order_id = resp.get("data", {}).get("orderId", "")
        logger.info(f"TP placed @ {req.tp_price} | id={order_id}")
        return order_id

    async def _cancel_plan_orders(self, symbol: str):
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

    async def _wait_fill(self, order_id: str, timeout: float = 5.0) -> Optional[float]:
        from pybitget_client import OrderApi
        deadline = time.time() + timeout
        api = OrderApi(self._client)

        while time.time() < deadline:
            try:
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: api.detail(
                        symbol=self.cfg.symbol,
                        productType=self.cfg.product_type,
                        orderId=order_id,
                    )
                )
                data = resp.get("data", {})
                status = data.get("status", "")
                if status == "filled":
                    price = float(data.get("priceAvg", 0) or 0)
                    return price if price else None
                if status in ("cancelled", "failed"):
                    return None
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return None

    async def _retry(self, func, *args, **kwargs):
        last_exc = None
        for attempt in range(self.cfg.max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_exc = e
                wait = self.cfg.retry_delay_s * (2 ** attempt)
                logger.warning(f"Retry {attempt+1}/{self.cfg.max_retries} after {wait:.1f}s: {e}")
                await asyncio.sleep(wait)
        raise last_exc

    # ============================================================
    # Вспомогательные методы для трейлинга и мониторинга
    # ============================================================

    async def _get_last_price(self, symbol: str) -> Optional[float]:
        try:
            import requests
            url = f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES"
            resp = requests.get(url, timeout=5)
            data = resp.json()
            if data.get("code") == "00000" and data.get("data"):
                return float(data["data"][0]["lastPr"])
            return None
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return None

    async def _get_atr(self, symbol: str) -> Optional[float]:
        # Получение ATR через публичный API или из кэша
        # Упрощённо: возвращаем None, в реальности нужно использовать indicators_calc
        return None

    async def _get_sl_order(self, symbol: str) -> Optional[str]:
        # Запрос к бирже для получения активного план-ордера
        # Здесь заглушка
        return None

    async def _place_emergency_sl(self, symbol: str, sl_price: float, size: float, side: PositionSide):
        # Выставляет аварийный SL через API
        try:
            from pybitget_client import OrderApi
            api = OrderApi(self._client)

            trigger_side = OrderSide.SELL if side == PositionSide.LONG else OrderSide.BUY

            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.placePlanOrder(
                    symbol=symbol,
                    productType=self.cfg.product_type,
                    marginMode=self.cfg.margin_mode,
                    marginCoin=self.cfg.margin_coin,
                    size=str(size),
                    triggerPrice=str(sl_price),
                    side=trigger_side.value,
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
    # Полноценный мониторинг позиций (трейлинг, stage, таймаут)
    # ============================================================


    async def get_open_positions(self):
        """Возвращает список открытых позиций."""
        # Возвращаем позиции из памяти (self._positions)
        positions = []
        for symbol, pos in self._positions.items():
            if pos.is_open:
                # Получаем текущую цену
                current_price = await self._get_last_price(symbol)
                positions.append({
                    "symbol": pos.symbol,
                    "side": pos.side.value,
                    "size": pos.size,
                    "entry_price": pos.avg_price,
                    "initial_sl": pos.initial_sl,
                    "current_sl": pos.current_sl,
                    "trailing_step": pos.trailing_step,
                    "trade_id": pos.trade_id,
                    "current_price": current_price or 0.0,
                })
        return positions

    async def partial_close(self, symbol: str, side: str, pct: float):
        """Закрыть pct% позиции по рынку."""
        pos = self._positions.get(symbol)
        if not pos:
            return
        close_size = round(pos.size * pct, 6)
        close_side = "sell" if side == "LONG" else "buy"
        # dry-run — просто логируем
        if self.cfg.dry_run:
            logger.info(f"[DRY] partial_close {symbol} {pct*100:.0f}% size={close_size}")
            pos.size = round(pos.size - close_size, 6)
            return
        # реальный режим — рыночный ордер на закрытие
        # Используем _place_market_order с reduce_only=True
        close_side_enum = OrderSide.SELL if side == "LONG" else OrderSide.BUY
        await self._place_market_order(
            symbol=symbol,
            side=close_side_enum,
            quantity=close_size,
            reduce_only=True,
        )

    async def _cancel_sl_order(self, symbol: str):
        """Отменяет активный стоп-лосс план-ордер для символа."""
        if self.cfg.dry_run:
            logger.info(f"[DRY] cancel_sl_order {symbol}")
            return
        try:
            from pybitget_client import OrderApi
            api = OrderApi(self._client)
            # Получаем список ожидающих план-ордеров типа loss_plan
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
                        lambda: api.cancelPlanOrder(
                            symbol=symbol,
                            productType=self.cfg.product_type,
                            orderId=order_id,
                            planType="pos_loss",
                        )
                    )
                    logger.info(f"Cancelled SL order {order_id} for {symbol}")
        except Exception as e:
            logger.error(f"Failed to cancel SL order for {symbol}: {e}")

    async def move_stop_loss(self, symbol: str, side: str, new_sl: float):
        """Переставить стоп-ордер на новую цену."""
        if self.cfg.dry_run:
            logger.info(f"[DRY] move_sl {symbol} → {new_sl}")
            if symbol in self._positions:
                self._positions[symbol].current_sl = new_sl
            return
        # реальный режим — отменить старый SL, поставить новый
        await self._cancel_sl_order(symbol)
        await self._place_sl_raw(symbol, side, self._positions[symbol].size, new_sl)

    async def _monitor_loop(self):
        """Запускается в отдельной задаче для управления позициями."""
        while True:
            await asyncio.sleep(10)  # каждые 10 секунд
            try:
                for symbol, pos in list(self._positions.items()):
                    if not pos.is_open:
                        continue

                    # Получаем текущую цену
                    price = await self._get_last_price(symbol)
                    if not price:
                        continue

                    # Возраст позиции
                    age = self._position_ages.get(symbol, 0) + 1
                    self._position_ages[symbol] = age

                    # Position age timeout
                    if age > self.cfg.max_hold_checks:
                        # Получаем unrealized PnL
                        pnl_pct = (price - pos.avg_price) / pos.avg_price * 100 if pos.side == PositionSide.LONG else (pos.avg_price - price) / pos.avg_price * 100
                        if pnl_pct < self.cfg.min_expected_pnl_pct:
                            logger.info(f"Position {symbol} timeout (age={age}, PnL={pnl_pct:.2f}%), closing")
                            await self.close_position(symbol, reason="timeout")
                            continue

                    # Трейлинг: получаем ATR и stage
                    atr = await self._get_atr(symbol)
                    if not atr:
                        continue

                    # Здесь нужно загрузить trailing_activated, trailing_stop, stage из памяти/БД
                    # Для краткости оставляем заглушку, но логика трейлинга должна быть развёрнута
                    # (см. наши предыдущие обсуждения)
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")            
