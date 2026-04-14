import asyncio
import logging
import signal
from pathlib import Path
from orchestrator import Orchestrator

# Создаем папку для логов если её нет
Path("logs").mkdir(exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("logs/dina.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Устанавливаем уровень логирования для data_feed в WARNING, чтобы уменьшить шум
logging.getLogger("data_feed").setLevel(logging.WARNING)

logger = logging.getLogger("main")


async def main():
    orch = Orchestrator()

    # Graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("Signal received — initiating graceful shutdown...")
        shutdown_event.set()

    # Register signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await orch._setup()
        await orch._start_all()

        # Wait for shutdown signal or KeyboardInterrupt
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — initiating graceful shutdown...")
    except Exception as exc:
        logger.exception("Critical error: %s", exc)
    finally:
        logger.info("Starting graceful shutdown (timeout 30s)...")
        try:
            await asyncio.wait_for(orch._stop_all(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Graceful shutdown timed out after 30s — forcing exit")
        except Exception as exc:
            logger.error("Error during shutdown: %s", exc)
        logger.info("Дина завершена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass