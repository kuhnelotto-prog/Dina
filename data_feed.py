"""
data_feed.py

WebSocket-подписка на свечи Bitget USDT-FUTURES.
Автоматический реконнект с экспоненциальным backoff.
Обновляет кэш свечей в SignalBuilder.
"""

import asyncio
import json
import logging
import pandas as pd
import websockets
from websockets.exceptions import ConnectionClosed
from typing import List, Dict

from signal_builder import SignalBuilder

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.bitget.com/v2/ws/public"

TF_MAP = {
    "15m": "15m",
    "1h":  "1H",
    "4h":  "4H",
    "1d":  "1D",
}

CANDLE_LIMIT = 200

class DataFeed:
    def __init__(
        self,
        symbols: List[str],
        timeframes: List[str],
        signal_builder: SignalBuilder,
    ):
        self.symbols = symbols
        self.timeframes = timeframes
        self.signal_builder = signal_builder
        self._running = False
        self._candle_buf: Dict[tuple, list] = {}

    async def start(self):
        self._running = True
        tasks = [
            asyncio.create_task(self._connect_loop(symbol, tf))
            for symbol in self.symbols
            for tf in self.timeframes
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self):
        self._running = False

    async def _connect_loop(self, symbol: str, tf: str):
        backoff = 5
        while self._running:
            try:
                await self._subscribe(symbol, tf)
                backoff = 5
            except (ConnectionClosed, OSError) as exc:
                logger.warning(
                    "DataFeed: %s %s обрыв: %s. Реконнект через %ds",
                    symbol, tf, exc, backoff,
                )
            except Exception as exc:
                logger.error(
                    "DataFeed: %s %s ошибка: %s. Реконнект через %ds",
                    symbol, tf, exc, backoff,
                )
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _subscribe(self, symbol: str, tf: str):
        bitget_tf = TF_MAP.get(tf, tf)
        subscribe_msg = json.dumps({
            "op": "subscribe",
            "args": [{
                "instType": "USDT-FUTURES",
                "channel": f"candle{bitget_tf}",
                "instId": symbol,
            }]
        })

        logger.info("DataFeed: подключение %s %s", symbol, tf)

        async with websockets.connect(
            WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            await ws.send(subscribe_msg)
            logger.info("DataFeed: подписан на %s %s", symbol, tf)

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    await ws.ping()
                    continue

                await self._handle_message(raw, symbol, tf)

    async def _handle_message(self, raw: str, symbol: str, tf: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if "event" in msg:
            return

        data = msg.get("data")
        if not data:
            return

        key = (symbol, tf)
        if key not in self._candle_buf:
            self._candle_buf[key] = []

        buf = self._candle_buf[key]

        for candle in data:
            if len(candle) < 6:
                continue
            ts = int(candle[0]) // 1000
            row = [ts] + [float(x) for x in candle[1:6]]

            if buf and buf[-1][0] == ts:
                buf[-1] = row
            else:
                buf.append(row)
                if len(buf) > CANDLE_LIMIT:
                    buf.pop(0)

        if len(buf) >= 30:
            df = pd.DataFrame(
                buf,
                columns=["ts", "open", "high", "low", "close", "volume"],
            )
            df["ts"] = pd.to_datetime(df["ts"], unit="s")
            df = df.set_index("ts")
            await self.signal_builder.update_candle(symbol, tf, df)
            logger.debug(
                "DataFeed: обновлён кэш %s %s (%d свечей)",
                symbol, tf, len(buf),
            )