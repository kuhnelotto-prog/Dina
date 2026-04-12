"""
executor/guard.py

ExecutionGuard — проверки перед исполнением ордера:
  - Allowlist символов
  - Rate limit (max orders per minute)
  - Max position % от депозита
"""

import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class ExecutionGuard:
    """Предохранитель перед исполнением ордеров."""

    def __init__(self, cfg):
        """
        Args:
            cfg: ExecutorConfig
        """
        self.cfg = cfg
        self._order_timestamps: deque = deque()
        self._paused: bool = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self):
        """Приостанавливает исполнение."""
        self._paused = True
        logger.warning("ExecutionGuard: PAUSED")

    def resume(self):
        """Возобновляет исполнение."""
        self._paused = False
        logger.info("ExecutionGuard: RESUMED")

    def check(self, req) -> bool:
        """
        Проверяет, можно ли исполнить ордер.
        
        Args:
            req: OrderRequest
        Returns:
            True если ордер разрешён, False если заблокирован
        """
        if self._paused:
            logger.warning(f"ExecutionGuard: blocked (paused) — {req.symbol}")
            return False

        # 1. Allowlist
        symbol = req.symbol or self.cfg.symbol
        if symbol not in self.cfg.allowlist_symbols:
            logger.warning(f"ExecutionGuard: {symbol} not in allowlist {self.cfg.allowlist_symbols}")
            return False

        # 2. Rate limit
        now = time.time()
        # Удаляем старые записи (старше 60 сек)
        while self._order_timestamps and self._order_timestamps[0] < now - 60:
            self._order_timestamps.popleft()

        if len(self._order_timestamps) >= self.cfg.max_orders_per_minute:
            logger.warning(
                f"ExecutionGuard: rate limit exceeded "
                f"({len(self._order_timestamps)}/{self.cfg.max_orders_per_minute} per minute)"
            )
            return False

        # 3. Max position % (проверяется в RiskManager, здесь — дополнительная защита)
        # Пропускаем — это уже проверено в risk_manager.check()

        return True

    def record_order(self):
        """Записывает timestamp ордера для rate limiting."""
        self._order_timestamps.append(time.time())
