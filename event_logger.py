"""
event_logger.py
Чистый журнал торговых событий → logs/events.log
Пишет только важное: сигналы, позиции, стопы, ошибки.
"""

import logging
import json
from pathlib import Path
from datetime import datetime

Path("logs").mkdir(exist_ok=True)

# Отдельный logger только для событий
_event_log = logging.getLogger("dina.events")
_event_log.setLevel(logging.INFO)
_event_log.propagate = False  # не дублировать в основной лог

_handler = logging.FileHandler("logs/events.log", encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_event_log.addHandler(_handler)

def _log(event_type: str, data: dict):
    data["event"] = event_type
    data["ts"] = datetime.utcnow().isoformat()
    _event_log.info(json.dumps(data, ensure_ascii=False))

# Публичные функции — вызывай их из любого модуля

def signal_generated(symbol: str, side: str, entry: float,
                     sl: float, tp: float, conf: float, tf: str):
    _log("SIGNAL", {
        "symbol": symbol, "side": side,
        "entry": entry, "sl": sl, "tp": tp,
        "conf": round(conf, 2), "tf": tf,
    })

def position_opened(symbol: str, side: str, size: float, entry: float):
    _log("POSITION_OPEN", {
        "symbol": symbol, "side": side,
        "size": size, "entry": entry,
    })

def position_closed(symbol: str, side: str, pnl: float = None):
    _log("POSITION_CLOSE", {
        "symbol": symbol, "side": side,
        "pnl": pnl,
    })

def trailing_stop_moved(symbol: str, old_sl: float, new_sl: float, step: int):
    _log("TRAILING_STOP", {
        "symbol": symbol,
        "old_sl": old_sl, "new_sl": new_sl, "step": step,
    })

def partial_close(symbol: str, pct: float, price: float, step: int):
    _log("PARTIAL_CLOSE", {
        "symbol": symbol,
        "pct": pct, "price": price, "step": step,
    })

def error(source: str, message: str):
    _log("ERROR", {"source": source, "message": message})