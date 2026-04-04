import asyncio
import logging
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

if __name__ == "__main__":
    asyncio.run(Orchestrator().run())
