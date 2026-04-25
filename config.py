"""
config.py — Единая точка конфигурации Дины.

Все переменные из .env загружаются здесь.
Импортируется: from config import settings

Не добавляйте os.getenv() в другие файлы — используйте settings.
"""

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()  # ← единственное место в проекте


# ============================================================
# Helpers
# ============================================================

def _require(key: str) -> str:
    """Обязательная переменная. Raises ValueError если не задана."""
    val = os.getenv(key)
    if not val:
        raise ValueError(f"[config] Обязательная переменная не задана: {key}")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _list(key: str, default: str = "") -> List[str]:
    raw = os.getenv(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# ============================================================
# Bitget API
# ============================================================

@dataclass
class BitgetConfig:
    api_key: str = field(default_factory=lambda: _optional("BITGET_API_KEY"))
    api_secret: str = field(default_factory=lambda: _optional("BITGET_API_SECRET"))
    passphrase: str = field(default_factory=lambda: _optional("BITGET_PASSPHRASE"))


# ============================================================
# Telegram
# ============================================================

@dataclass
class TelegramConfig:
    bot_token: str = field(default_factory=lambda: _optional("TELEGRAM_BOT_TOKEN"))
    allowed_ids: List[int] = field(default_factory=lambda: [
        int(x) for x in _list("TELEGRAM_ALLOWED_IDS") if x
    ])
    # Ночной режим (UTC)
    silent_start_hour: int = field(default_factory=lambda: _int("SILENT_HOURS_START", 23))
    silent_end_hour: int = field(default_factory=lambda: _int("SILENT_HOURS_END", 7))


# ============================================================
# Trading
# ============================================================

@dataclass
class TradingConfig:
    dry_run: bool = field(default_factory=lambda: _optional("DRY_RUN", "true").lower() == "true")
    starting_balance: float = field(default_factory=lambda: _float("STARTING_BALANCE", 10000))
    symbols: List[str] = field(default_factory=lambda: _list("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,SOLUSDT,AVAXUSDT,ADAUSDT,SUIUSDT,APEUSDT,ARBUSDT"))
    timeframes: List[str] = field(default_factory=lambda: _list("TIMEFRAMES", "15m,1h,4h"))
    timeframe_weights: List[float] = field(default_factory=lambda: [
        float(x) for x in _list("TIMEFRAME_WEIGHTS", "0.2,0.3,0.5")
    ])
    leverage: int = field(default_factory=lambda: _int("LEVERAGE", 3))
    db_path: str = field(default_factory=lambda: _optional("DB_PATH", "dina.db"))


# ============================================================
# Risk Management
# ============================================================

@dataclass
class RiskConfig:
    base_risk_pct: float = field(default_factory=lambda: _float("BASE_RISK_PCT", 1.0))
    max_risk_pct: float = field(default_factory=lambda: _float("MAX_RISK_PCT", 2.0))
    max_positions: int = field(default_factory=lambda: _int("MAX_POSITIONS", 1))
    daily_loss_limit: float = field(default_factory=lambda: _float("DAILY_LOSS_LIMIT", 5.0))
    max_consecutive_losses: int = field(default_factory=lambda: _int("MAX_CONSECUTIVE_LOSSES", 5))
    max_total_exposure_usd: float = field(default_factory=lambda: _float("MAX_TOTAL_EXPOSURE", 50000.0))


# ============================================================
# Trailing
# ============================================================

# ── 4-step ATR Trailing Stages ──
# ⚠️ ВНИМАНИЕ: TRAILING_STAGES используется ТОЛЬКО в trailing_manager.py (живая система).
# backtester.py с P34 использует ДРУГУЮ логику выхода (TP1/TP2/TSL от пика).
# Это расхождение нужно устранить перед переходом в продакшен.
# partial_close_pct = доля ОРИГИНАЛЬНОЙ позиции для закрытия (не текущего остатка!)
# Система автоматически пересчитывает в долю от текущего остатка.
TRAILING_STAGES = [
    {"stage": 1, "activation_atr": 0.5, "sl_atr": 0.0,  "partial_close_pct": 0.0,  "description": "breakeven"},
    {"stage": 2, "activation_atr": 1.0, "sl_atr": 0.5,  "partial_close_pct": 0.25, "description": "close 25%"},
    {"stage": 3, "activation_atr": 1.5, "sl_atr": 1.0,  "partial_close_pct": 0.25, "description": "close 25%"},
    {"stage": 4, "activation_atr": 2.0, "sl_atr": None,  "partial_close_pct": 1.0,  "description": "close all"},
]

# ── P34: Asymmetric LONG/SHORT parameters ──
# Используется в: backtester.py. ⚠️ Должно быть синхронизировано с trailing_manager.py перед продакшеном!
SL_ATR_MULT_LONG = 6.6     # wider SL for longs (survive corrections in staircase pattern)
SL_ATR_MULT_SHORT = 1.5    # standard SL for shorts
TSL_ATR_LONG_STEP0 = 0     # disabled: no trailing before TP1 for LONG
TSL_ATR_LONG_AFTER_TP1 = 2.0  # softer trailing after TP1 for LONG
TSL_ATR_SHORT = 1.5         # TSL distance for SHORT (from peak)


@dataclass
class TrailingConfig:
    activation_atr: float = field(default_factory=lambda: _float("TRAILING_ACTIVATION_ATR", 0.5))
    step_atr: float = field(default_factory=lambda: _float("TRAILING_STEP_ATR", 0.2))
    dist_atr: float = field(default_factory=lambda: _float("TRAILING_DIST_ATR", 1.2))


# ============================================================
# LLM Filter (DeepSeek)
# ============================================================

@dataclass
class LLMConfig:
    provider: str = field(default_factory=lambda: _optional("LLM_PROVIDER", "deepseek"))
    deepseek_mode: str = field(default_factory=lambda: _optional("DEEPSEEK_MODE", "ollama"))
    deepseek_api_key: str = field(default_factory=lambda: _optional("DEEPSEEK_API_KEY"))
    ollama_url: str = field(default_factory=lambda: _optional("OLLAMA_URL", "http://localhost:11434/api/chat"))
    deepseek_model: str = field(default_factory=lambda: _optional("DEEPSEEK_MODEL", "deepseek-v3"))


# ============================================================
# Email (SMTP fallback)
# ============================================================

@dataclass
class EmailConfig:
    smtp_host: str = field(default_factory=lambda: _optional("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _optional("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _optional("SMTP_PASSWORD"))
    alert_email_to: str = field(default_factory=lambda: _optional("ALERT_EMAIL_TO"))


# ============================================================
# Optional APIs
# ============================================================

@dataclass
class OptionalAPIsConfig:
    whale_alert_api_key: str = field(default_factory=lambda: _optional("WHALE_ALERT_API_KEY"))
    fred_api_key: str = field(default_factory=lambda: _optional("FRED_API_KEY"))


# ============================================================
# Safety Guard
# ============================================================

@dataclass
class SafetyConfig:
    max_fast_drawdown_pct: float = field(default_factory=lambda: _float("MAX_FAST_DRAWDOWN_PCT", 3.0))
    max_position_age_hours: float = field(default_factory=lambda: _float("MAX_POSITION_AGE_HOURS", 48.0))
    heartbeat_timeout_sec: int = field(default_factory=lambda: _int("HEARTBEAT_TIMEOUT_SEC", 60))
    dry_run: bool = field(default_factory=lambda: _optional("SAFETY_GUARD_DRY_RUN", "true").lower() == "true")


# ============================================================
# Composite Settings
# ============================================================

@dataclass
class Settings:
    """Единый конфиг всего проекта. Импортируется: from config import settings"""

    bitget: BitgetConfig = field(default_factory=BitgetConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    optional_apis: OptionalAPIsConfig = field(default_factory=OptionalAPIsConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)


settings = Settings()  # singleton