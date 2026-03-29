"""
main.py

Точка входа. Собирает все модули и запускает бота.
"""

import asyncio
import logging
import os
import time
from dotenv import load_dotenv

load_dotenv()

from position_sizer import PortfolioState, SizerConfig
from risk_manager import RiskManager
from bitget_executor import BitgetExecutor, ExecutorConfig
from strategist_client import StrategistClient
from telegram_bot import DinaBot, TelegramConfig
from performance_attribution import PerformanceAttribution
from event_bus import EventBus
from signal_builder import SignalBuilder
from learning_engine import LearningEngine
from data_feed import DataFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("Дина: старт")

    # Конфигурация
    symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
    timeframes = os.getenv("TIMEFRAMES", "15m,1h,4h").split(",")
    starting_balance = float(os.getenv("STARTING_BALANCE", 10000))

    # Инициализация компонентов
    bus = EventBus()
    attribution = PerformanceAttribution()
    await attribution.setup()

    portfolio = PortfolioState(balance=starting_balance, peak_balance=starting_balance)

    risk_manager = RiskManager(
        sizer_config=SizerConfig(
            base_risk_pct=float(os.getenv("BASE_RISK_PCT", 1.0)),
            max_risk_pct=float(os.getenv("MAX_RISK_PCT", 2.0)),
        ),
        max_open_positions=int(os.getenv("MAX_POSITIONS", 1)),
        daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT", 5.0)),
        max_total_exposure_usd=float(os.getenv("MAX_TOTAL_EXPOSURE", 5000.0)),
    )

    executor = BitgetExecutor(ExecutorConfig(
        symbol=symbols[0],
        leverage=int(os.getenv("LEVERAGE", 3)),
    ))
    await executor.setup()

    bot = DinaBot(
        config=TelegramConfig(),
        risk_manager=risk_manager,
        portfolio=portfolio,
        executor=executor,
        attribution=attribution,
        symbols=symbols,
    )
    await bot.setup()

    # Создаём LearningEngine
    learning_engine = LearningEngine()
    await learning_engine.setup()

    # Создаём SignalBuilder
    signal_builder = SignalBuilder(
        symbols=symbols,
        timeframes=timeframes,
        learning=learning_engine,
        direction="LONG",      # можно параметризовать
        bus=bus,
    )

    # Создаём DataFeed
    data_feed = DataFeed(symbols, timeframes, signal_builder)

    strategist = StrategistClient(
        bus=bus,
        symbols=symbols,
        timeframes=timeframes,
        signal_builder=signal_builder,
        learning_engine=learning_engine,
        attribution=attribution,
        risk_manager=risk_manager,
        portfolio=portfolio,
        executor=executor,
        bot=bot,
        direction="LONG",
        tiered_confidence_full=0.75,
        tiered_confidence_half=0.55,
    )

    bot.strategist = strategist

    # Запуск задач
    logger.info("Дина: все модули готовы, запускаю")

    bus_task = asyncio.create_task(bus.run())
    trade_task = asyncio.create_task(strategist.run_loop())
    tg_task = asyncio.create_task(bot.run())
    data_task = asyncio.create_task(data_feed.start())
    summary_task = asyncio.create_task(_daily_summary(bot))

    await asyncio.gather(bus_task, trade_task, tg_task, data_task, summary_task)


async def _daily_summary(bot: DinaBot):
    while True:
        now = time.gmtime()
        seconds_till = ((23 - now.tm_hour) * 3600 + (59 - now.tm_min) * 60 + (0 - now.tm_sec)) % 86400
        await asyncio.sleep(seconds_till)
        await bot.alert_daily_summary()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Дина: остановлена")
