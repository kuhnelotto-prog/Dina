"""
orchestrator.py — Координатор Дины.

Только запуск и остановка компонентов.
Вся логика мониторинга — в position_monitor.py.
Вся логика трейлинга — в trailing_manager.py.

Использование в main.py:
    from orchestrator import Orchestrator
    asyncio.run(Orchestrator().run())
"""

import asyncio
import logging
import os
from typing import Dict

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
from trailing_manager import TrailingManager
from position_monitor import PositionMonitor

logger = logging.getLogger("Orchestrator")


class Orchestrator:
    """
    Координатор всех компонентов Дины.
    Только инициализация, запуск и остановка.
    """

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

        # Компоненты — инициализируются в _setup()
        self.bus: EventBus | None = None
        self.executor: BitgetExecutor | None = None
        self.bot: DinaBot | None = None
        self.safety_guard = None
        self.strategist_long: StrategistClient | None = None
        self.strategist_short: StrategistClient | None = None
        self.data_feed: DataFeed | None = None
        self.position_monitor: PositionMonitor | None = None
        self.trailing_manager: TrailingManager | None = None
        self.portfolio: PortfolioState | None = None
        self.risk_manager: RiskManager | None = None

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

        symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,XRPUSDT,DOGEUSDT,LINKUSDT").split(",")
        timeframes = os.getenv("TIMEFRAMES", "15m,1h,4h,1d").split(",")
        starting_balance = float(os.getenv("STARTING_BALANCE", 10000))

        # EventBus
        self.bus = EventBus()

        # PerformanceAttribution
        attribution = PerformanceAttribution()
        await attribution.setup()

        # BitgetExecutor
        self.executor = BitgetExecutor(ExecutorConfig(
            symbol=symbols[0],
            leverage=int(os.getenv("LEVERAGE", 3)),
        ))
        await self.executor.setup()

        # Баланс с биржи
        try:
            starting_balance = await self.executor.get_balance()
            if starting_balance <= 0:
                raise ValueError("нулевой баланс")
        except Exception as e:
            logger.warning(f"Не удалось получить баланс с биржи: {e} — используем .env")
            starting_balance = float(os.getenv("STARTING_BALANCE", 10000))

        # Portfolio
        self.portfolio = PortfolioState(
            balance=starting_balance,
            peak_balance=starting_balance,
        )
        logger.info(f"💰 Стартовый баланс: ${starting_balance:.2f}")

        # RiskManager
        self.risk_manager = RiskManager(
            sizer_config=SizerConfig(
                base_risk_pct=float(os.getenv("BASE_RISK_PCT", 1.0)),
                max_risk_pct=float(os.getenv("MAX_RISK_PCT", 2.0)),
                leverage=int(os.getenv("LEVERAGE", 3)),
            ),
            max_open_positions=int(os.getenv("MAX_POSITIONS", 2)),
            daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT", 5.0)),
            max_total_exposure_usd=float(os.getenv("MAX_TOTAL_EXPOSURE", 5000.0)),
        )

        # Восстановление позиций с биржи
        await self._reconcile_positions()

        # LearningEngine
        learning_engine = LearningEngine()
        await learning_engine.setup()

        # SignalBuilders (shared cooldown)
        shared_signal_time: Dict[str, float] = {}

        signal_builder_long = SignalBuilder(
            symbols=symbols, timeframes=timeframes,
            learning=learning_engine, direction="LONG",
            bus=self.bus, shared_signal_time=shared_signal_time,
        )
        signal_builder_short = SignalBuilder(
            symbols=symbols, timeframes=timeframes,
            learning=learning_engine, direction="SHORT",
            bus=self.bus, shared_signal_time=shared_signal_time,
        )

        # Telegram
        self.bot = DinaBot(
            config=TelegramConfig(),
            main_loop=asyncio.get_running_loop(),
            risk_manager=self.risk_manager,
            portfolio=self.portfolio,
            executor=self.executor,
            attribution=attribution,
            symbols=symbols,
        )
        await self.bot.setup()

        if self.bot:
            startup_msg = "🚀 Дина запущена (dry‑run режим)" if self.executor.cfg.dry_run else "🚀 Дина запущена"
            try:
                await self.bot._send(startup_msg, priority="info")
            except Exception as e:
                logger.warning(f"Orchestrator: не удалось отправить startup сообщение в Telegram: {e}")
                logger.info("Orchestrator: продолжаем без Telegram уведомления")

        # DataFeed — оба SignalBuilder получают данные + risk_manager получает 4H для корреляции
        self.data_feed = DataFeed(
            symbols, timeframes,
            signal_builders=[signal_builder_long, signal_builder_short],
            risk_manager=self.risk_manager,
        )

        # Дина-Лонг
        self.strategist_long = StrategistClient(
            bus=self.bus, symbols=symbols, timeframes=timeframes,
            signal_builder=signal_builder_long,
            learning_engine=learning_engine, attribution=attribution,
            risk_manager=self.risk_manager, portfolio=self.portfolio,
            executor=self.executor, bot=self.bot,
            direction="LONG",
        )

        # Дина-Шорт
        self.strategist_short = StrategistClient(
            bus=self.bus, symbols=symbols, timeframes=timeframes,
            signal_builder=signal_builder_short,
            learning_engine=learning_engine, attribution=attribution,
            risk_manager=self.risk_manager, portfolio=self.portfolio,
            executor=self.executor, bot=self.bot,
            direction="SHORT",
        )

        # TrailingManager (с risk_manager для синхронизации partial close)
        self.trailing_manager = TrailingManager(
            executor=self.executor,
            bot=self.bot,
            risk_manager=self.risk_manager,
        )

        # PositionMonitor
        self.position_monitor = PositionMonitor(
            executor=self.executor,
            trailing_manager=self.trailing_manager,
            portfolio=self.portfolio,
            risk_manager=self.risk_manager,
            strategist_long=self.strategist_long,
            strategist_short=self.strategist_short,
            bot=self.bot,
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

    async def _reconcile_positions(self) -> None:
        """Восстанавливает позиции с биржи после рестарта."""
        try:
            await self.executor._reconcile()
            positions = await self.executor.get_open_positions()
            for pos in positions:
                symbol = pos["symbol"]
                side = pos.get("side", "long")
                size_usd = pos.get("size", 0) * pos.get("entry_price", 0)
                self.risk_manager.on_trade_opened(symbol, size_usd, side, direction=side)
            if positions:
                logger.info(f"Восстановлено {len(positions)} позиций в risk_manager")
        except Exception as e:
            logger.warning(f"Не удалось восстановить позиции: {e}")

    # ──────────────────────────────────────────────
    # Запуск задач
    # ──────────────────────────────────────────────

    async def _start_all(self) -> None:
        logger.info("Оркестратор: запуск задач...")
        self._tasks = []

        # Дина-Лонг
        self._tasks.append(asyncio.create_task(
            self._run_with_restart(self.strategist_long.run_loop, "Дина-Лонг"),
            name="dina-long",
        ))

        # Дина-Шорт
        self._tasks.append(asyncio.create_task(
            self._run_with_restart(self.strategist_short.run_loop, "Дина-Шорт"),
            name="dina-short",
        ))

        # PositionMonitor
        self._tasks.append(asyncio.create_task(
            self._run_with_restart(self.position_monitor.run, "PositionMonitor"),
            name="position-monitor",
        ))

        # SafetyGuard
        self._tasks.append(asyncio.create_task(
            self.safety_guard.run(),
            name="safety-guard",
        ))

        # Heartbeat
        self._tasks.append(asyncio.create_task(
            self._heartbeat_loop(),
            name="heartbeat",
        ))

        # DataFeed
        self._tasks.append(asyncio.create_task(
            self._run_with_restart(self.data_feed.start, "DataFeed"),
            name="data-feed",
        ))

        # Telegram
        if self.bot:
            self._tasks.append(asyncio.create_task(
                self._run_with_restart(self.bot.run, "DinaBot"),
                name="telegram-bot",
            ))

        logger.info("Оркестратор: %d задач запущено ✅", len(self._tasks))

    # ──────────────────────────────────────────────
    # Остановка
    # ──────────────────────────────────────────────

    async def _stop_all(self) -> None:
        logger.info("Оркестратор: остановка всех задач...")

        if self.position_monitor:
            self.position_monitor.stop()
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
    # Вспомогательные
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

    async def _run_with_restart(self, coro_fn, name: str) -> None:
        """Запускает корутину с авто-рестартом (макс 10 раз)."""
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
                logger.error("%s: упал (попытка %d/%d): %s", name, restarts, max_restarts, exc)
                if restarts > max_restarts:
                    logger.critical("%s: превышен лимит перезапусков — останавливаю систему", name)
                    self._shutdown_event.set()
                    return
                wait = min(10 * restarts, 60)
                logger.info("%s: перезапуск через %d сек...", name, wait)
                await asyncio.sleep(wait)

    async def _run_telegram(self) -> None:
        """Запуск Telegram бота в отдельном потоке."""
        try:
            await asyncio.to_thread(self.bot.run_sync)
        except Exception as exc:
            logger.error("DinaBot error: %s", exc)


# ──────────────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    asyncio.run(Orchestrator().run())