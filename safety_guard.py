class SafetyGuardConfig:
    def __init__(self, max_fast_drawdown, max_position_age, heartbeat_threshold, no_sl):
        self.max_fast_drawdown = max_fast_drawdown
        self.max_position_age = max_position_age
        self.heartbeat_threshold = heartbeat_threshold
        self.no_sl = no_sl

class SafetyGuard:
    def __init__(self, config: SafetyGuardConfig):
        self.config = config

    def _check_fast_drawdown(self, current_value, previous_value):
        return (previous_value - current_value) / previous_value <= self.config.max_fast_drawdown

    def _check_position_age(self, position_age):
        return position_age <= self.config.max_position_age

    def _check_heartbeat(self, last_heartbeat):
        return (datetime.utcnow() - last_heartbeat).total_seconds() <= self.config.heartbeat_threshold

    def _check_no_sl(self):
        return self.config.no_sl

def create_safety_guard(max_fast_drawdown, max_position_age, heartbeat_threshold, no_sl):
    config = SafetyGuardConfig(max_fast_drawdown, max_position_age, heartbeat_threshold, no_sl)
    return SafetyGuard(config)