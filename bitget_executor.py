"""
bitget_executor.py — Фасад для исполнения ордеров на Bitget Futures.

Делегирует логику модулям из executor/:
  - executor.api_client — низкоуровневые API вызовы
  - executor.order_manager — открытие/закрытие позиций
  - executor.trailing — трейлинг-стоп
  - executor.reconciliation — сверка позиций
  - executor.guard — execution guard

Сохраняет обратную совместимость: все публичные методы остаются на месте.
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
    max_position_pct: float = 0.15
    max_orders_per_minute: int = 5

    # Trailing
    trailing_activation_atr: float = 0.5
    trailing_step_atr: float = 0.2
    trailing_dist_atr: float = 1.2

    # Position age timeout
    base_timeout_hours: float = 48.0
    mid_timeout_hours: float = 72.0
    max_timeout_hours: float = 96.0
    timeout_atr_mid: float = 1.0
    timeout_atr_max: float = 2.0
    min_expected_pnl_pct: float = 0.5
    max_hold_checks: int = 48


# ============================================================
# Модели (остаются здесь для обратной совместимости)
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
    direction: str
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
    commission: float = 0.0
    commission_asset: str = "USDT"

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
    initial_sl: float = 0.0
    current_sl: float = 0.0
    trailing_step: int = 0

    @property
    def is_open(self) -> bool:
        return self.side != PositionSide.NONE and self.size > 0


# ============================================================
# BitgetExecutor — Фасад
# ============================================================

class BitgetExecutor:
    """
    Фасад для работы с биржей Bitget.
    Делегирует логику модулям из executor/.
    Сохраняет полную обратную совместимость.
    """

    def __init__(self, config: Optional[ExecutorConfig] = None):
        self.cfg = config or ExecutorConfig()
        self._client: Optional[Any] = None
        self._positions: Dict[str, PositionInfo] = {}
        self._position_ages: Dict[str, int] = {}
        self._strategist: Optional[Any] = None

        # Подмодули — инициализируются в setup()
        self._api_client = None
        self._order_mgr = None
        self._trailing_mgr = None
        self._reconciliation_mgr = None
        self._guard = None

        logger.info(f"BitgetExecutor init | symbol={self.cfg.symbol} leverage={self.cfg.leverage}x dry_run={self.cfg.dry_run}")

    def set_strategist(self, strategist):
        self._strategist = strategist

    # ============================================================
    # Setup
    # ============================================================

    async def setup(self):
        """Создаёт таблицы, устанавливает плечо, инициализирует подмодули."""
        await self._init_db()

        if self.cfg.dry_run:
            logger.warning("BitgetExecutor: DRY RUN mode — ордера не исполняются")
        else:
            try:
                from pybitget_client import Client
                self._client = Client(
                    api_key=self.cfg.api_key,
                    api_secret=self.cfg.api_secret,
                    passphrase=self.cfg.passphrase,
                )
            except ImportError:
                raise RuntimeError("python-bitget не установлен. Запусти: pip install python-bitget")

        # Инициализация подмодулей
        from executor.api_client import BitgetAPIClient
        from executor.order_manager import OrderManager
        from executor.trailing import ExecutorTrailingManager
        from executor.reconciliation import ReconciliationManager
        from executor.guard import ExecutionGuard

        self._api_client = BitgetAPIClient(self._client, self.cfg)
        self._guard = ExecutionGuard(self.cfg)
        self._order_mgr = OrderManager(self._api_client, self.cfg, self._positions, self.cfg.db_path)
        self._trailing_mgr = ExecutorTrailingManager(
            self._api_client, self.cfg, self._positions, self._position_ages, self.cfg.db_path
        )
        self._reconciliation_mgr = ReconciliationManager(
            self._api_client, self.cfg, self._positions, self._trailing_mgr
        )

        if not self.cfg.dry_run:
            await self._api_client.set_leverage()
            await self._reconciliation_mgr.reconcile()
            logger.info("BitgetExecutor: подключен к Bitget")

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
                    error TEXT,
                    dry_run BOOLEAN,
                    trade_id TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS active_trailing (
                    symbol TEXT PRIMARY KEY,
                    activated BOOLEAN,
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
                    avg_price REAL,
                    initial_sl REAL,
                    current_sl REAL,
                    trailing_step INTEGER DEFAULT 0,
                    trade_id TEXT,
                    opened_at REAL
                )
            """)
            await db.commit()

    # ============================================================
    # Публичные методы — делегируют подмодулям
    # ============================================================

    async def open_position(self, req: OrderRequest) -> OrderResult:
        """Открывает позицию: entry + SL + TP."""
        # Guard check
        if not self._guard.check(req):
            return OrderResult(success=False, error="Blocked by ExecutionGuard")
        self._guard.record_order()
        return await self._order_mgr.open_position(req)

    async def close_position(self, symbol: str, reason: str = "signal") -> OrderResult:
        """Закрывает позицию полностью."""
        return await self._order_mgr.close_position(symbol, reason)

    async def partial_close(self, symbol: str, side: str, pct: float):
        """Закрыть pct% позиции по рынку."""
        await self._order_mgr.partial_close(symbol, side, pct)

    async def move_stop_loss(self, symbol: str, side: str, new_sl: float):
        """Переставить стоп-ордер на новую цену."""
        await self._trailing_mgr.move_stop_loss(symbol, side, new_sl)

    async def get_position(self, symbol: str) -> PositionInfo:
        """Возвращает информацию о позиции из памяти."""
        return self._positions.get(symbol, PositionInfo(symbol=symbol, side=PositionSide.NONE))

    async def get_positions_from_exchange(self) -> List[PositionInfo]:
        """Получает позиции с биржи."""
        raw = await self._api_client.get_positions_from_exchange()
        result = []
        for p in raw:
            symbol = p.get("symbol", "")
            hold_side = p.get("holdSide", "long").lower()
            side = PositionSide.LONG if hold_side == "long" else PositionSide.SHORT
            result.append(PositionInfo(
                symbol=symbol,
                side=side,
                size=float(p.get("total", 0) or 0),
                avg_price=float(p.get("openPriceAvg", 0) or p.get("averageOpenPrice", 0) or 0),
                unrealized_pnl=float(p.get("unrealisedPnl", 0) or p.get("unrealizedPnl", 0) or 0),
                leverage=int(p.get("leverage", self.cfg.leverage) or self.cfg.leverage),
                margin=float(p.get("margin", 0) or p.get("marginSize", 0) or 0),
            ))
        return result

    async def get_open_positions(self):
        """Возвращает список открытых позиций из памяти."""
        positions = []
        for symbol, pos in self._positions.items():
            if pos.is_open:
                current_price = await self._api_client.get_last_price(symbol)
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

    async def get_balance(self) -> float:
        """Получает баланс аккаунта."""
        if self.cfg.dry_run:
            return float(os.getenv("STARTING_BALANCE", 10000))
        return await self._api_client.get_balance()

    async def get_funding_rate(self, symbol: str) -> float:
        """Получает текущий funding rate."""
        return await self._api_client.get_funding_rate(symbol)

    async def place_stop_loss(self, symbol: str, side: str, quantity: float,
                              sl_price: float) -> str:
        """Публичный метод для размещения SL."""
        if self.cfg.dry_run:
            return f"dry_sl_{uuid.uuid4().hex[:6]}"
        return await self._api_client.place_sl(symbol, side, quantity, sl_price)

    # ============================================================
    # Reconciliation
    # ============================================================

    async def _reconcile(self):
        """Восстанавливает позиции с биржи."""
        if self._reconciliation_mgr:
            await self._reconciliation_mgr.reconcile()

    # ============================================================
    # Trailing state persistence (делегируем)
    # ============================================================

    async def _save_trailing_state(self, symbol: str, activated: bool,
                                   trailing_stop: float, stage: int,
                                   plan_order_id: str):
        await self._trailing_mgr.save_trailing_state(symbol, activated, trailing_stop, stage, plan_order_id)

    async def _restore_trailing_state(self, symbol: str):
        return await self._trailing_mgr.restore_trailing_state(symbol)

    async def _clear_trailing_state(self, symbol: str):
        await self._trailing_mgr.clear_trailing_state(symbol)

    # ============================================================
    # Вспомогательные (делегируем)
    # ============================================================

    async def _get_last_price(self, symbol: str) -> Optional[float]:
        return await self._api_client.get_last_price(symbol)

    async def _get_atr(self, symbol: str) -> Optional[float]:
        return None  # заглушка

    async def _get_sl_order(self, symbol: str) -> Optional[str]:
        return await self._api_client.get_active_sl(symbol) or None

    async def _place_emergency_sl(self, symbol: str, sl_price: float,
                                  size: float, side: PositionSide):
        await self._api_client.place_emergency_sl(symbol, sl_price, size, side.value)

    def _get_dynamic_timeout_hours(self, pos: PositionInfo, current_price: float) -> float:
        return self._trailing_mgr.get_dynamic_timeout_hours(pos, current_price)

    def _calc_quantity(self, size_usd: float, price: float) -> float:
        return self._order_mgr.calc_quantity(size_usd, price)

    def _execution_guard_check(self, req: OrderRequest) -> bool:
        return self._guard.check(req)

    async def _cancel_sl_order(self, symbol: str):
        if self.cfg.dry_run:
            logger.info(f"[DRY] cancel_sl_order {symbol}")
            return
        await self._api_client.cancel_sl_order(symbol)

    async def _cancel_plan_orders(self, symbol: str):
        await self._api_client.cancel_plan_orders(symbol)

    async def _monitor_loop(self):
        """Запускается в отдельной задаче для управления позициями."""
        await self._trailing_mgr.monitor_loop(self.close_position)

    async def _retry(self, func, *args, **kwargs):
        """Retry с экспоненциальным backoff."""
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
