from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

class Priority(Enum):
    CRITICAL = 0
    HIGH     = 1
    NORMAL   = 2
    LOW      = 3

class EventType(str, Enum):
    MARKET_DATA        = "market_data"
    CANDLE_CLOSED      = "candle_closed"
    TRADE_SIGNAL       = "trade_signal"
    TRADE_OPENED       = "trade_opened"
    TRADE_CLOSED       = "trade_closed"
    RISK_BLOCK         = "risk_block"
    RISK_VIOLATION     = "risk_violation"
    WHALE_SIGNAL       = "whale_signal"
    WHALE_ALERT        = "whale_alert"
    MACRO_SIGNAL       = "macro_signal"
    EXCHANGE_DEGRADED  = "exchange_degraded"
    BOT_COMMAND        = "bot_command"
    HEALTH_CHECK       = "health_check"
    TRADING_PAUSED     = "trading_paused"

@dataclass
class BotEvent:
    type: EventType
    data: Any
    priority: Priority = Priority.NORMAL
    symbol: Optional[str] = None

    def __str__(self):
        return f"[{self.priority.name}] {self.type.value} {self.symbol or ''}"