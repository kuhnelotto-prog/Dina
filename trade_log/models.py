from dataclasses import dataclass, asdict
from typing import Optional, List, Any
import json
import time

@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    side: str
    size_usd: float
    entry_price: float
    entry_time: float
    bot_id: str = "dina_long"
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    duration_min: Optional[float] = None
    exit_reason: str = ""
    tags: Optional[List[str]] = None
    source: str = "live"          # backtest, dryrun, live
    commission: float = 0.0
    commission_asset: str = "USDT"
    setup_type: str = ""          # trend_continuation, trend_reversal, breakout, fvg, sweep, unknown
    signals_fired: Optional[dict] = None  # P38: raw signal components at entry (e.g. {"rsi": 0.4, "macd": 1.0, ...})
    composite_score: float = 0.0  # P38: entry composite score

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if isinstance(self.tags, str):
            try:
                self.tags = json.loads(self.tags)
            except:
                self.tags = []

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tags"] = json.dumps(self.tags)
        d["signals_fired"] = json.dumps(self.signals_fired) if self.signals_fired else None
        return d

    @classmethod
    def from_dict(cls, data: dict):
        if "tags" in data and isinstance(data["tags"], str):
            try:
                data["tags"] = json.loads(data["tags"])
            except:
                data["tags"] = []
        if "signals_fired" in data and isinstance(data["signals_fired"], str):
            try:
                data["signals_fired"] = json.loads(data["signals_fired"])
            except:
                data["signals_fired"] = None
        return cls(**data)

    @property
    def is_closed(self) -> bool:
        return self.exit_time is not None

    @property
    def is_win(self) -> bool:
        return self.pnl_usd is not None and self.pnl_usd > 0

@dataclass
class PnLSummary:
    period: str
    trades: int
    wins: int
    losses: int
    total_pnl: float
    win_rate: float
    best_trade: Optional[float] = None
    worst_trade: Optional[float] = None
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}