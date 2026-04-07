
"""
safety_guard.py — Независимый asyncio-watchdog для Дины.

Запуск в main.py:
    sg = create_safety_guard(executor, telegram)
    asyncio.create_task(sg.run())

Основной бот в своём цикле вызывает:
    sg.heartbeat()
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("SafetyGuard")

@dataclass
class SafetyGuardConfig:
    max_fast_drawdown_pct: float = float(os.getenv("MAX_FAST_DRAWDOWN_PCT", 3.0))
    max_position_age_hours: float = float(os.getenv("MAX_POSITION_AGE_HOURS", 48.0))
    heartbeat_timeout_sec: int = int(os.getenv("HEARTBEAT_TIMEOUT_SEC", 60))
    emergency_sl_atr_multiplier: float = 1.5
    check_interval_sec: int = 30
    dry_run: bool = os.getenv("SAFETY_GUARD_DRY_RUN", "false").lower() == "true"
    alert_cooldown_sec: int = 300

class SafetyGuard:
    def __init__(self, executor, telegram=None,
                 config: Optional[SafetyGuardConfig] = None):
        self.executor = executor
        self.telegram = telegram
        self.cfg = config or SafetyGuardConfig()
        self._last_heartbeat: float = time.monotonic()
        self._heartbeat_alerted: bool = False
        self._alerted_at: dict = {}
        self._running: bool = False

    # ── Публичный интерфейс ──────────────────────

    def heartbeat(self) -> None:
        """Основной бот вызывает этот метод в своём главном цикле."""
        self._last_heartbeat = time.monotonic()
        self._heartbeat_alerted = False

    async def run(self) -> None:
        self._running = True
        logger.info("SafetyGuard запущен. dry_run=%s", self.cfg.dry_run)
        while self._running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.exception("SafetyGuard: необработанная ошибка: %s", exc)
            await asyncio.sleep(self.cfg.check_interval_sec)

    def stop(self) -> None:
        self._running = False

    # ── Цикл ────────────────────────────────────

    async def _cycle(self) -> None:
        positions = await self._get_open_positions()
        await self._check_heartbeat()
        for pos in positions:
            symbol = pos.get("symbol", "?")
            try:
                await self._check_fast_drawdown(pos)
                await self._check_position_age(pos)
                await self._check_no_sl(pos)
            except Exception as exc:
                logger.error("SafetyGuard: ошибка %s: %s", symbol, exc)

    # ── Проверка 1: быстрая просадка ────────────

    async def _check_fast_drawdown(self, pos: dict) -> None:
        symbol = pos.get("symbol", "?")
        side = pos.get("holdSide") or pos.get("side", "long")
        entry = float(pos.get("openPriceAvg") or pos.get("averageOpenPrice") or pos.get("entry_price") or 0)
        mark = float(pos.get("markPrice") or pos.get("current_price") or 0)

        if entry <= 0 or mark <= 0:
            return

        pnl_pct = ((mark - entry) / entry * 100) if side == "long" \
                  else ((entry - mark) / entry * 100)

        # уточняем через unrealisedPnl если есть
        unrealised = float(pos.get("unrealisedPnl") or 0)
        qty = float(pos.get("total") or 0)
        if qty > 0 and entry > 0:
            pnl_pct = min(pnl_pct, unrealised / (qty * entry) * 100)
            
        if pnl_pct <= -self.cfg.max_fast_drawdown_pct:
            msg = (f"⚠️ DRAWDOWN: {symbol} {side.upper()} "
                   f"просадка {pnl_pct:.2f}% "
                   f"(лимит -{self.cfg.max_fast_drawdown_pct}%) → закрываю")
            logger.warning(msg)
            await self._notify(symbol, msg, level="high")
            await self._force_close(symbol, side, reason="fast_drawdown")

    # ── Проверка 2: возраст позиции ─────────────

    async def _check_position_age(self, pos: dict) -> None:
        symbol = pos.get("symbol", "?")
        side = pos.get("holdSide", "long")
        open_ts_ms = int(pos.get("cTime") or pos.get("createTime") or 0)

        if open_ts_ms <= 0:
            return

        age_hours = (time.time() - open_ts_ms / 1000) / 3600

        if age_hours >= self.cfg.max_position_age_hours:
            msg = (f"⏰ AGE LIMIT: {symbol} {side.upper()} "
                   f"открыта {age_hours:.1f}ч "
                   f"(лимит {self.cfg.max_position_age_hours}ч) → закрываю")
            logger.warning(msg)
            await self._notify(symbol, msg, level="high")
            await self._force_close(symbol, side, reason="age_limit")

    # ── Проверка 3: heartbeat ────────────────────

    async def _check_heartbeat(self) -> None:
        silence = time.monotonic() - self._last_heartbeat

        if silence >= self.cfg.heartbeat_timeout_sec:
            if not self._heartbeat_alerted:
                msg = (f"🚨 CRITICAL: основной бот молчит {silence:.0f}с "
                       f"(лимит {self.cfg.heartbeat_timeout_sec}с). "
                       f"SafetyGuard работает.")
                logger.critical(msg)
                await self._notify("SYSTEM", msg, level="critical", force=True)
                self._heartbeat_alerted = True
        else:
            if self._heartbeat_alerted:
                logger.info("SafetyGuard: heartbeat восстановлен")
                self._heartbeat_alerted = False

    # ── Проверка 4: отсутствие SL ───────────────

    async def _check_no_sl(self, pos: dict) -> None:
        symbol = pos.get("symbol", "?")
        side = pos.get("holdSide", "long")
        sl_price = float(pos.get("stopLossPrice") or pos.get("stopLoss") or 0)

        if sl_price > 0:
            return  # SL есть, всё хорошо

        entry = float(pos.get("openPriceAvg") or pos.get("averageOpenPrice") or 0)
        if entry <= 0:
            return

        atr = await self._get_atr(symbol, entry)
        sl_dist = self.cfg.emergency_sl_atr_multiplier * atr
        emergency_sl = (entry - sl_dist) if side == "long" else (entry + sl_dist)

        msg = (f"🛡️ EMERGENCY SL: {symbol} {side.upper()} "
               f"SL отсутствует! "
               f"Выставляю SL={emergency_sl:.4f} "
               f"(entry={entry:.4f})")
        logger.warning(msg)
        await self._notify(symbol, msg, level="critical", force=True)

        if not self.cfg.dry_run:
            try:
                await self.executor.place_stop_loss(
                    symbol=symbol, side=side, sl_price=emergency_sl)
                logger.info("SafetyGuard: SL выставлен %s @ %.4f", symbol, emergency_sl)
            except Exception as exc:
                logger.error("SafetyGuard: не удалось выставить SL %s: %s", symbol, exc)
        else:
            logger.info("SafetyGuard [DRY-RUN]: SL не выставлен для %s", symbol)

    # ── Вспомогательные ─────────────────────────
    async def _get_open_positions(self) -> list:
        try:
            return await self.executor.get_open_positions() or []
        except Exception as exc:
            logger.error("SafetyGuard: не удалось получить позиции: %s", exc)
            return []

    async def _get_atr(self, symbol: str, entry: float) -> float:
        try:
            if hasattr(self.executor, "get_atr"):
                atr = await self.executor.get_atr(symbol)
                if atr and atr > 0:
                    return atr
        except Exception:
            pass
        return entry * 0.005  # fallback: 0.5% от цены входа

    async def _force_close(self, symbol: str, side: str, reason: str) -> None:
        if self.cfg.dry_run:
            logger.info("SafetyGuard [DRY-RUN]: закрытие %s пропущено (%s)",
                        symbol, reason)
            return
        try:
            await self.executor.close_position(
                symbol=symbol, reason=reason)
            logger.info("SafetyGuard: закрыта %s %s (%s)", symbol, side, reason)
        except Exception as exc:
            logger.error("SafetyGuard: не удалось закрыть %s: %s", symbol, exc)

    async def _notify(self, symbol: str, message: str,
                      level: str = "high", force: bool = False) -> None:
        now = time.monotonic()
        last = self._alerted_at.get(symbol, 0)
        if not force and (now - last) < self.cfg.alert_cooldown_sec:
            return
        self._alerted_at[symbol] = now

        if self.telegram is None:
            logger.warning("SafetyGuard [no-telegram]: %s", message)
            return
        try:
            await self.telegram.send_alert(message=message, level=level)
        except Exception as exc:
            logger.warning("SafetyGuard: Telegram недоступен (%s): %s",
                           exc, message)

def create_safety_guard(executor, telegram=None, config=None) -> SafetyGuard:
    return SafetyGuard(executor=executor, telegram=telegram, config=config)
