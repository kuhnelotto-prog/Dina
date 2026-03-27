"""
data_feed.py

Управление вебсокет-подписками для получения свечей по разным таймфреймам.
Обновляет кэш свечей в SignalBuilder.
"""

import asyncio
import logging
import json
from typing import List, Tuple, Optional
import pandas as pd
import websockets

from signal_builder import SignalBuilder

logger = logging.getLogger(__name__)


class DataFeed:
    def __init__(self, symbols: List[str], timeframes: List[str], signal_builder: SignalBuilder):
        self.symbols = symbols
        self.timeframes = timeframes
        self.signal_builder = signal_builder
        self._running = False
        self._ws_connections = {}

    async def start(self):
        """Запускает подписки для всех символов и таймфреймов."""
        self._running = True
        tasks = []
        for symbol in self.symbols:
            for tf in self.timeframes:
                tasks.append(self._subscribe(symbol, tf))
        await asyncio.gather(*tasks)

    async def stop(self):
        self._running = False
        for ws in self._ws_connections.values():
            await ws.close()

    async def _subscribe(self, symbol: str, timeframe: str):
        """Подписка на один канал (упрощённая заглушка)."""
        # Для реального использования нужен правильный URL и формат Bitget
        # Здесь заглушка, чтобы не блокировать запуск
        logger.info(f"DataFeed: subscribing to {symbol} {timeframe} (stub)")
        while self._running:
            await asyncio.sleep(60)  # имитация получения данных
            # В реальном коде здесь будет обработка вебсокета