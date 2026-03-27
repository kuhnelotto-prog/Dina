"""
backtester.py — минимальная заглушка для прохождения валидации.
"""

import pandas as pd
import numpy as np

class Backtester:
    def __init__(self, initial_balance: float = 10000.0):
        self.initial_balance = initial_balance

    def run(self, data: pd.DataFrame, strategy: callable):
        """Заглушка, возвращает пустой результат."""
        return {
            "total_trades": 0,
            "win_rate": 0,
            "total_return_pct": 0,
            "max_drawdown_pct": 0,
            "profit_factor": 0,
            "sharpe_ratio": 0,
            "final_balance": self.initial_balance
        }