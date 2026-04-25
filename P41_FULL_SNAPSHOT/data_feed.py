"""
data_feed.py

WebSocket-подписка на свечи Bitget USDT-FUTURES.
Одно соединение — все подписки (до 240 каналов).
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

# Обратная карта для парсинга ответов
TF_REVERSE = {v: k for k, v in TF_MAP.items()}

CANDLE_LIMIT = 200


class DataFeed:
    def __init__(
        self,
        symbols: List[str],
        timeframes: List[str],
        signal_builders: "List[SignalBuilder] | SignalBuilder" = None,
        signal_builder: "SignalBuilder | None" = None,
        risk_manager=None,
    ):
        self.symbols = symbols
        self.timeframes = timeframes
        # Support both single signal_builder (backward compat) and list
        if signal_builders is not None:
            self._signal_builders = signal_builders if isinstance(signal_builders, list) else [signal_builders]
        elif signal_builder is not None:
            self._signal_builders = [signal_builder]
        else:
            self._signal_builders = []
        self._risk_manager = risk_manager
        self._running = False
        self._candle_buf: Dict[tuple, list] = {}
        self._ws = None

    async def start(self):
        self._running = True
        logger.info("DataFeed: запуск (%d символов × %d таймфреймов)", len(self.symbols), len(self.timeframes))
        await self._connect_loop()

    async def stop(self):
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _connect_loop(self):
        """Одно соединение с авто-реконнектом."""
        backoff = 5
        while self._running:
            try:
                await self._run_single_connection()
                backoff = 5  # сброс при успешном соединении
            except (ConnectionClosed, OSError) as exc:
                logger.warning(
                    "DataFeed: обрыв соединения: %s. Реконнект через %ds",
                    exc, backoff,
                )
            except Exception as exc:
                logger.error(
                    "DataFeed: ошибка: %s. Реконнект через %ds",
                    exc, backoff,
                )
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _run_single_connection(self):
        """Одно WebSocket соединение со всеми подписками."""
        logger.warning("DataFeed: подключение к %s ...", WS_URL)

        ws = await asyncio.wait_for(
            websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ),
            timeout=15,
        )
        try:
            self._ws = ws

            # Подписываемся на все символы и таймфреймы
            args = []
            for symbol in self.symbols:
                for tf in self.timeframes:
                    bitget_tf = TF_MAP.get(tf, tf)
                    args.append({
                        "instType": "USDT-FUTURES",
                        "channel": f"candle{bitget_tf}",
                        "instId": symbol,
                    })

            # Bitget позволяет до 240 подписок за раз
            # Отправляем батчами по 30 (на всякий случай)
            batch_size = 30
            for i in range(0, len(args), batch_size):
                batch = args[i:i + batch_size]
                subscribe_msg = json.dumps({"op": "subscribe", "args": batch})
                await ws.send(subscribe_msg)
                logger.info(
                    "DataFeed: подписка отправлена (%d каналов, батч %d/%d)",
                    len(batch), i // batch_size + 1,
                    (len(args) + batch_size - 1) // batch_size,
                )
                # Небольшая пауза между батчами
                if i + batch_size < len(args):
                    await asyncio.sleep(0.5)

            total = len(self.symbols) * len(self.timeframes)
            logger.info(
                "DataFeed: подписан на %d каналов (%d символов × %d таймфреймов) через 1 соединение",
                total, len(self.symbols), len(self.timeframes),
            )

            # Читаем сообщения
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    # Отправляем текстовый ping (Bitget формат)
                    await ws.send("ping")
                    continue

                raw_str = raw.decode() if isinstance(raw, bytes) else raw

                # Bitget отправляет текстовый "ping" — отвечаем "pong"
                if raw_str == "ping":
                    await ws.send("pong")
                    continue

                await self._handle_message(raw_str)
        finally:
            await ws.close()

    async def _handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Подтверждение подписки
        if "event" in msg:
            event = msg.get("event")
            if event == "error":
                logger.error("DataFeed: ошибка подписки: %s", msg)
            return

        # Данные свечей
        arg = msg.get("arg", {})
        data = msg.get("data")
        if not data or not arg:
            return

        symbol = arg.get("instId", "")
        channel = arg.get("channel", "")

        # Извлекаем таймфрейм из канала (candle15m → 15m, candle1H → 1h)
        tf = self._parse_tf(channel)
        if not tf:
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
            for sb in self._signal_builders:
                await sb.update_candle(symbol, tf, df)

            # Передаём 4H свечи в RiskManager для расчёта корреляции секторов
            if tf == "4h" and self._risk_manager is not None:
                self._risk_manager.update_candles(symbol, df)

            logger.debug(
                "DataFeed: обновлён кэш %s %s (%d свечей)",
                symbol, tf, len(buf),
            )

    @staticmethod
    def _parse_tf(channel: str) -> str:
        """candle15m → 15m, candle1H → 1h, candle4H → 4h"""
        if not channel.startswith("candle"):
            return ""
        bitget_tf = channel[6:]  # убираем "candle"
        # Проверяем обратную карту
        if bitget_tf in TF_REVERSE:
            return TF_REVERSE[bitget_tf]
        # Если не нашли — возвращаем как есть (lowercase)
        return bitget_tf.lower()
