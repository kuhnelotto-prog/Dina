import asyncio
import logging
from collections import defaultdict
from typing import Callable, Coroutine

from .models import BotEvent

log = logging.getLogger("EventBus")

class EventBus:
    def __init__(self):
        self._subscribers = defaultdict(list)
        self._queue = asyncio.Queue()
        self._running = False
        self._task = None

    def subscribe(self, event_type: str, callback: Callable[[BotEvent], Coroutine]):
        self._subscribers[event_type].append(callback)

    async def emit(self, event: BotEvent):
        await self._queue.put(event)

    async def _process_queue(self):
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch(event)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error(f"EventBus error: {e}")

    async def _dispatch(self, event: BotEvent):
        callbacks = self._subscribers.get(event.type, [])
        for cb in callbacks:
            try:
                await cb(event)
            except Exception as e:
                log.error(f"Subscriber error for {event.type}: {e}")

    async def run(self):
        self._running = True
        self._task = asyncio.create_task(self._process_queue())
        log.info("EventBus started")
        await self._task

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass