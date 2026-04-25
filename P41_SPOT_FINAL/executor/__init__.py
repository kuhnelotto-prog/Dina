"""
executor/ — Модульный пакет для работы с биржей Bitget.

Компоненты:
  - api_client.py — низкоуровневые вызовы pybitget API
  - order_manager.py — открытие/закрытие позиций, ордера
  - trailing.py — трейлинг-стоп логика
  - reconciliation.py — сверка позиций с биржей
  - guard.py — execution guard, rate limit, allowlist
"""

from executor.api_client import BitgetAPIClient
from executor.order_manager import OrderManager
from executor.trailing import ExecutorTrailingManager
from executor.reconciliation import ReconciliationManager
from executor.guard import ExecutionGuard

__all__ = [
    "BitgetAPIClient",
    "OrderManager",
    "ExecutorTrailingManager",
    "ReconciliationManager",
    "ExecutionGuard",
]
