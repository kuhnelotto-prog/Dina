from .models import Priority

EVENT_CONFIG = {
    "market_data":        (Priority.NORMAL, 0),
    "candle_closed":      (Priority.NORMAL, 0),
    "trade_signal":       (Priority.HIGH, 30),
    "trade_opened":       (Priority.HIGH, 0),
    "trade_closed":       (Priority.HIGH, 0),
    "risk_block":         (Priority.CRITICAL, 0),
    "risk_violation":     (Priority.HIGH, 60),
    "whale_signal":       (Priority.HIGH, 0),
    "whale_alert":        (Priority.CRITICAL, 0),
    "macro_signal":       (Priority.NORMAL, 900),
    "exchange_degraded":  (Priority.HIGH, 30),
    "bot_command":        (Priority.HIGH, 0),
    "health_check":       (Priority.NORMAL, 0),
    "trading_paused":     (Priority.HIGH, 0),
}

def get_priority(event_type: str) -> Priority:
    return EVENT_CONFIG.get(event_type, (Priority.NORMAL, 0))[0]

def get_cooldown(event_type: str) -> int:
    return EVENT_CONFIG.get(event_type, (Priority.NORMAL, 0))[1]