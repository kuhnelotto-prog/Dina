"""
orchestrator.py — Координатор Дины.

Запускает параллельно:
  - Дина-Лонг  (StrategistClient direction="LONG")
  - Дина-Шорт  (StrategistClient direction="SHORT")
  - SafetyGuard (независимый watchdog)
  - DataFeed    (поставка данных)

Использование в main.py:
    from orchestrator import Orchestrator
    asyncio.run(Orchestrator().run())
"""

import asyncio
import logging
import os
import signal
import time
from typing import Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from event_bus import EventBus
from performance_attribution import PerformanceAttribution
from position_sizer import PortfolioState, SizerConfig
from risk_manager import RiskManager
from bitget_executor import BitgetExecutor, ExecutorConfig
from telegram_bot import DinaBot, TelegramConfig
from learning_engine import LearningEngine
from signal_builder import SignalBuilder
from strategist_client import StrategistClient
from safety_guard import create_safety_guard, SafetyGuardConfig
from data_feed import DataFeed
import event_logger

logger = logging.getLogger("Orchestrator")

class Orchestrator:
    """
    Координатор всех компонентов Дины.

    Архитектура:
        DataFeed ──► SignalBuilder ──► StrategistLong  ──► BitgetExecutor
                                   └─► StrategistShort ──► BitgetExecutor
        SafetyGuard (независимо мониторит позиции)
        DinaBot     (Telegram-управление)
    """

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

        # Компоненты — инициализируются в setup()
        self.bus: EventBus | None = None
        self.executor: BitgetExecutor | None = None
        self.bot: DinaBot | None = None
        self.safety_guard = None
        self.strategist_long: StrategistClient | None = None
        self.strategist_short: StrategistClient | None = None
        self.data_feed: DataFeed | None = None

        # Монитор позиций
        self._monitor_running = False
        self._last_known_positions: dict[str, dict] = {}
        self._event_log: list[dict] = []

    # ──────────────────────────────────────────────
    # Точка входа
    # ──────────────────────────────────────────────

    async def run(self) -> None:
        """Главный метод. Вызывается из main.py."""
        logger.info("=" * 50)
        logger.info("Дина Оркестратор: запуск")
        logger.info("=" * 50)

        try:
            await self._setup()
            await self._start_all()
            await self._shutdown_event.wait()
        except KeyboardInterrupt:
            logger.info("Оркестратор: получен сигнал остановки")
        except Exception as exc:
            logger.exception("Оркестратор: критическая ошибка: %s", exc)
        finally:
            await self._stop_all()
            logger.info("Оркестратор: завершён")

    # ──────────────────────────────────────────────
    # Инициализация компонентов
    # ──────────────────────────────────────────────

    async def _setup(self) -> None:
        logger.info("Оркестратор: инициализация компонентов...")

        symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,"
                                       "XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,"
                                       "LINKUSDT,DOTUSDT").split(",")
        timeframes = os.getenv("TIMEFRAMES", "15m,1h,4h").split(",")
        starting_balance = float(os.getenv("STARTING_BALANCE", 10000))

        # EventBus
        self.bus = EventBus()

        # PerformanceAttribution
        attribution = PerformanceAttribution()
        await attribution.setup()

        # Portfolio
        portfolio = PortfolioState(
            balance=starting_balance,
            peak_balance=starting_balance
        )

        # RiskManager — общий для лонга и шорта
        risk_manager = RiskManager(
            sizer_config=SizerConfig(
                base_risk_pct=float(os.getenv("BASE_RISK_PCT", 1.0)),
                max_risk_pct=float(os.getenv("MAX_RISK_PCT", 2.0)),
            ),
            max_open_positions=int(os.getenv("MAX_POSITIONS", 2)),
            daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT", 5.0)),
            max_total_exposure_usd=float(os.getenv("MAX_TOTAL_EXPOSURE", 5000.0)),
        )

        # BitgetExecutor — общий
        self.executor = BitgetExecutor(ExecutorConfig(
            symbol=symbols[0],
            leverage=int(os.getenv("LEVERAGE", 3)),
        ))
        await self.executor.setup()
        await self._load_state_from_exchange()

        # LearningEngine
        learning_engine = LearningEngine()
        await learning_engine.setup()

        # SignalBuilder — общий для обоих направлений
        # Общий словарь для отслеживания времени последних сигналов
        shared_signal_time: Dict[str, float] = {}
        
        signal_builder_long = SignalBuilder(
            symbols=symbols,
            timeframes=timeframes,
            learning=learning_engine,
            direction="LONG",
            bus=self.bus,
            shared_signal_time=shared_signal_time,
        )
        signal_builder_short = SignalBuilder(
            symbols=symbols,
            timeframes=timeframes,
            learning=learning_engine,
            direction="SHORT",
            bus=self.bus,
            shared_signal_time=shared_signal_time,
        )

        # DinaBot (Telegram)
        self.bot = DinaBot(
            config=TelegramConfig(),
            main_loop=asyncio.get_running_loop(),
            risk_manager=risk_manager,
            portfolio=portfolio,
            executor=self.executor,
            attribution=attribution,
            symbols=symbols,
        )
        await self.bot.setup()
        
        # Отправляем уведомление о старте
        if self.bot:
            startup_msg = "🚀 Дина запущена (dry‑run режим)" if self.executor.cfg.dry_run else "🚀 Дина запущена"
            await self.bot._send(startup_msg, priority="info")

        # DataFeed — поставка данных
        self.data_feed = DataFeed(symbols, timeframes, signal_builder_long)

        # Дина-Лонг
        self.strategist_long = StrategistClient(
            bus=self.bus,
            symbols=symbols,
            timeframes=timeframes,
            signal_builder=signal_builder_long,
            learning_engine=learning_engine,
            attribution=attribution,
            risk_manager=risk_manager,
            portfolio=portfolio,
            executor=self.executor,
            bot=self.bot,
            direction="LONG",
            tiered_confidence_full=float(os.getenv("LONG_CONF_FULL", 0.75)),
            tiered_confidence_half=float(os.getenv("LONG_CONF_HALF", 0.55)),
        )

        # Дина-Шорт (более жёсткие фильтры — выше порог уверенности)
        self.strategist_short = StrategistClient(
            bus=self.bus,
            symbols=symbols,
            timeframes=timeframes,
            signal_builder=signal_builder_short,
            learning_engine=learning_engine,
            attribution=attribution,
            risk_manager=risk_manager,
            portfolio=portfolio,
            executor=self.executor,
            bot=self.bot,
            direction="SHORT",
            tiered_confidence_full=float(os.getenv("SHORT_CONF_FULL", 0.80)),
            tiered_confidence_half=float(os.getenv("SHORT_CONF_HALF", 0.65)),
        )

        # SafetyGuard
        sg_config = SafetyGuardConfig(
            max_fast_drawdown_pct=float(os.getenv("MAX_FAST_DRAWDOWN_PCT", 3.0)),
            max_position_age_hours=float(os.getenv("MAX_POSITION_AGE_HOURS", 48.0)),
            heartbeat_timeout_sec=int(os.getenv("HEARTBEAT_TIMEOUT_SEC", 60)),
            dry_run=os.getenv("SAFETY_GUARD_DRY_RUN", "true").lower() == "true",
        )
        self.safety_guard = create_safety_guard(
            executor=self.executor,
            telegram=self.bot,
            config=sg_config,
        )

        logger.info("Оркестратор: все компоненты готовы ✅")
        logger.info("  Символов: %d | Таймфреймов: %d", len(symbols), len(timeframes))
        logger.info("  Дина-Лонг порог: %.2f / %.2f",
                    self.strategist_long.tiered_confidence_full,
                    self.strategist_long.tiered_confidence_half)
        logger.info("  Дина-Шорт порог: %.2f / %.2f",
                    self.strategist_short.tiered_confidence_full,
                    self.strategist_short.tiered_confidence_half)
    async def _load_state_from_exchange(self) -> None:
        """Восстанавливает позиции и трейлинговые состояния после рестарта."""
        if self.executor:
            await self.executor._reconcile()

    # ──────────────────────────────────────────────
    # Запуск задач
    # ──────────────────────────────────────────────

    async def _start_all(self) -> None:
        logger.info("Оркестратор: запуск задач...")

        self._tasks = [
            asyncio.create_task(
                self._run_with_restart(
                    self.strategist_long.run_loop, "Дина-Лонг"),
                name="dina-long"),
            asyncio.create_task(
                self._run_with_restart(
                    self.strategist_short.run_loop, "Дина-Шорт"),
                name="dina-short"),
            asyncio.create_task(
                self.safety_guard.run(),
                name="safety-guard"),
            asyncio.create_task(
                self._heartbeat_loop(),
                name="heartbeat"),
        ]

        # DataFeed если есть метод run
        if hasattr(self.data_feed, "start"):
            self._tasks.append(
                asyncio.create_task(
                    self._run_with_restart(self.data_feed.start, "DataFeed"),
                    name="data-feed")
            )

        # Telegram bot если есть
        if hasattr(self.bot, "run"):
            self._tasks.append(
                asyncio.create_task(
                    self._run_with_restart(self._run_telegram, "DinaBot"),
                    name="telegram-bot")
            )

        logger.info("Оркестратор: %d задач запущено ✅", len(self._tasks))

        # Запускаем монитор позиций
        asyncio.create_task(self._position_monitor_loop())

    # ──────────────────────────────────────────────
    # Heartbeat — сигнал жизни для SafetyGuard
    # ──────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Каждые 30 сек сообщает SafetyGuard что система жива."""
        while True:
            try:
                if self.safety_guard:
                    self.safety_guard.heartbeat()
            except Exception as exc:
                logger.warning("Heartbeat error: %s", exc)
            await asyncio.sleep(30)

    # ──────────────────────────────────────────────
    # Авто-рестарт упавших задач
    # ──────────────────────────────────────────────

    async def _run_with_restart(self, coro_fn, name: str) -> None:
        """
        Запускает корутину. Если упала — ждёт 10 сек и перезапускает.
        Максимум 10 перезапусков, потом завершает работу.
        """
        restarts = 0
        max_restarts = 10

        while restarts <= max_restarts:
            try:
                await coro_fn()
                logger.info("%s: завершился нормально", name)
                return
            except asyncio.CancelledError:
                logger.info("%s: отменён", name)
                return
            except Exception as exc:
                restarts += 1
                logger.error(
                    "%s: упал (попытка %d/%d): %s",
                    name, restarts, max_restarts, exc
                )
                if restarts > max_restarts:
                    logger.critical(
                        "%s: превышен лимит перезапусков — останавливаю систему",
                        name
                    )
                    self._shutdown_event.set()
                    return
                wait = min(10 * restarts, 60)
                logger.info("%s: перезапуск через %d сек...", name, wait)
                await asyncio.sleep(wait)

    # ──────────────────────────────────────────────
    # Остановка
    # ──────────────────────────────────────────────


    async def _run_telegram(self) -> None:
        """Запуск Telegram бота."""
        try:
            if hasattr(self.bot, "run_sync"):
                # Запускаем бота в отдельном потоке, чтобы избежать конфликтов с event loop
                await asyncio.to_thread(self.bot.run_sync)
        except Exception as exc:
            logger.error("DinaBot error: %s", exc)
            # Не поднимаем исключение дальше, чтобы не вызывать перезапуски
            # Telegram бот либо работает, либо нет, но не должен ломать всю систему

    async def _stop_all(self) -> None:
        logger.info("Оркестратор: остановка всех задач...")

        # Останавливаем монитор позиций
        self._monitor_running = False

        if self.safety_guard:
            self.safety_guard.stop()

        if self.strategist_long:
            await self.strategist_long.stop()

        if self.strategist_short:
            await self.strategist_short.stop()
        if self.bot:
            self.bot.stop()

        if self.data_feed:
            await self.data_feed.stop()            

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("Оркестратор: все задачи остановлены ✅")

    # ──────────────────────────────────────────────
    # Монитор позиций
    # ──────────────────────────────────────────────

    async def _position_monitor_loop(self):
        """
        Фоновый монитор позиций. Работает вечно в отдельном таске.
        Ничего не изменяет, только наблюдает, логирует и оповещает об изменениях.
        """
        self._monitor_running = True
        logger.info("🔍 Монитор позиций запущен")

        while self._monitor_running:
            try:
                # Запрашиваем актуальные позиции из биржи
                positions = await self.executor.get_open_positions()
                current_symbols = {p["symbol"] for p in positions}
                last_symbols = set(self._last_known_positions.keys())

                # ✅ Новая позиция открылась
                for symbol in current_symbols - last_symbols:
                    pos = next(p for p in positions if p["symbol"] == symbol)
                    event = {
                        "ts": asyncio.get_event_loop().time(),
                        "type": "position_opened",
                        "symbol": symbol,
                        "side": pos["side"],
                        "size": pos["size"],
                        "entry_price": pos["entry_price"],
                    }
                    self._event_log.append(event)
                    logger.info(f"✅ Новая позиция: {pos['side']} {symbol} {pos['size']} @ {pos['entry_price']}")
                    await self.bot._send(f"✅ Открыта позиция\n{pos['side']} {symbol}\nРазмер: {pos['size']}\nВход: {pos['entry_price']:.2f}", priority="info")
                    # Логируем событие
                    event_logger.position_opened(
                        symbol=symbol,
                        side=pos["side"],
                        size=pos["size"],
                        entry=pos["entry_price"]
                    )

                # ❌ Позиция закрылась
                for symbol in last_symbols - current_symbols:
                    pos = self._last_known_positions[symbol]
                    event = {
                        "ts": asyncio.get_event_loop().time(),
                        "type": "position_closed",
                        "symbol": symbol,
                        "side": pos["side"],
                    }
                    self._event_log.append(event)
                    logger.info(f"❌ Позиция закрыта: {pos['side']} {symbol}")
                    await self.bot._send(f"❌ Позиция закрыта\n{pos['side']} {symbol}", priority="info")
                    # Логируем событие
                    event_logger.position_closed(
                        symbol=symbol,
                        side=pos["side"]
                    )

                # Обновляем состояние
                self._last_known_positions = {p["symbol"]: p for p in positions}

            except Exception as exc:
                logger.warning(f"⚠ Монитор позиций: ошибка: {exc}")

            finally:
                await asyncio.sleep(10)

# ──────────────────────────────────────────────────────
# Точка входа (если запускается напрямую)
# ──────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    asyncio.run(Orchestrator().run())
