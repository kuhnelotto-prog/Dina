"""
position_sizer.py

Динамический расчёт размера позиции для Дины.

Факторы:
  - Базовый риск (Kelly или фиксированный)
  - Волатильность (ATR)
  - Уверенность LLM-фильтра
  - Просадка портфеля
  - Серия проигрышей
"""

import math
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class SizerDecision(str, Enum):
    TRADE = "TRADE"
    REDUCE = "REDUCE"
    HALT = "HALT"


@dataclass
class SizerConfig:
    # Базовый риск (% от баланса)
    base_risk_pct: float = 1.0
    max_risk_pct: float = 2.0
    min_risk_pct: float = 0.25

    # Волатильность — нормальный ATR (% от цены)
    normal_atr_pct: float = 1.5

    # Просадка — пороги
    drawdown_start_pct: float = 5.0
    drawdown_halt_pct: float = 15.0

    # Серия проигрышей
    consec_loss_start: int = 3
    consec_loss_halt: int = 6

    # Confidence фильтра
    conf_min: float = 0.65
    conf_max: float = 0.95

    # Kelly fraction (0 = выключен)
    kelly_fraction: float = 0.25

    # Плечо (умножение размера позиции)
    leverage: int = 1


@dataclass
class PortfolioState:
    balance: float = 10000.0
    peak_balance: float = 10000.0
    consecutive_losses: int = 0
    total_trades: int = 0
    recent_pnl: list = field(default_factory=list)

    @property
    def drawdown_pct(self) -> float:
        if self.peak_balance <= 0:
            return 0.0
        return (self.peak_balance - self.balance) / self.peak_balance * 100

    def update(self, pnl_usd: float):
        self.balance += pnl_usd
        self.peak_balance = max(self.peak_balance, self.balance)
        self.total_trades += 1
        self.recent_pnl.append(pnl_usd)
        if len(self.recent_pnl) > 20:
            self.recent_pnl = self.recent_pnl[-20:]

        if pnl_usd < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0


@dataclass
class SizeResult:
    decision: SizerDecision
    risk_pct: float = 0.0
    position_usd: float = 0.0
    units: float = 0.0

    base_risk_pct: float = 0.0
    vol_multiplier: float = 1.0
    conf_multiplier: float = 1.0
    drawdown_multiplier: float = 1.0
    streak_multiplier: float = 1.0
    halt_reason: str = ""

    def __str__(self):
        if self.decision == SizerDecision.HALT:
            return f"[HALT] {self.halt_reason}"
        tag = "⚠️ REDUCE" if self.decision == SizerDecision.REDUCE else "✅ TRADE"
        return (
            f"{tag} | risk={self.risk_pct:.2f}% | pos=${self.position_usd:,.0f} | "
            f"vol×{self.vol_multiplier:.2f} conf×{self.conf_multiplier:.2f} "
            f"dd×{self.drawdown_multiplier:.2f} streak×{self.streak_multiplier:.2f}"
        )


class PositionSizer:
    def __init__(self, config: Optional[SizerConfig] = None):
        self.cfg = config or SizerConfig()
        logger.info(f"PositionSizer init | base={self.cfg.base_risk_pct}% max={self.cfg.max_risk_pct}%")

    def calculate(
        self,
        portfolio: PortfolioState,
        entry_price: float,
        sl_price: float,
        confidence: float,
        atr_pct: Optional[float] = None,
        win_rate: Optional[float] = None,
        avg_rr: Optional[float] = None,
        side: str = "long",
    ) -> SizeResult:
        cfg = self.cfg

        # 1. Просадка: HALT если превышен лимит
        dd = portfolio.drawdown_pct
        if dd >= cfg.drawdown_halt_pct:
            return SizeResult(
                decision=SizerDecision.HALT,
                halt_reason=f"Drawdown {dd:.1f}% ≥ limit {cfg.drawdown_halt_pct}%"
            )

        # 2. Серия проигрышей: HALT
        cl = portfolio.consecutive_losses
        if cl >= cfg.consec_loss_halt:
            return SizeResult(
                decision=SizerDecision.HALT,
                halt_reason=f"{cl} losses in row ≥ {cfg.consec_loss_halt}"
            )

        # 3. Базовый риск (Kelly или фиксированный)
        if win_rate is not None and avg_rr is not None and win_rate > 0 and avg_rr > 0:
            base_risk = self._kelly_risk(win_rate, avg_rr)
        else:
            base_risk = cfg.base_risk_pct

        # 4. Множители
        vol_mult = self._vol_multiplier(atr_pct)
        conf_mult = self._conf_multiplier(confidence)
        dd_mult = self._drawdown_multiplier(dd)
        streak_mult = self._streak_multiplier(cl)

        # 5. Итоговый риск
        raw_risk = base_risk * vol_mult * conf_mult * dd_mult * streak_mult
        # Жёсткое ограничение сверху (с epsilon для float)
        epsilon = 1e-6
        if raw_risk > cfg.max_risk_pct + epsilon:
            logger.warning(
                f"PositionSizer: raw_risk {raw_risk:.2f}% превышает max_risk_pct {cfg.max_risk_pct}%, "
                f"обрезаем. Множители: base={base_risk:.2f} vol={vol_mult:.2f} conf={conf_mult:.2f} "
                f"dd={dd_mult:.2f} streak={streak_mult:.2f}"
            )
            raw_risk = cfg.max_risk_pct
        risk_pct = max(cfg.min_risk_pct, raw_risk)

                                        # 6. Расчёт позиции через SL distance
        # Проверка направления стоп-лосса
        if side.lower() == "long" and sl_price >= entry_price:
            return SizeResult(
                decision=SizerDecision.HALT,
                halt_reason=f"Invalid SL for long: SL={sl_price} >= Entry={entry_price}"
            )
        if side.lower() == "short" and sl_price <= entry_price:
            return SizeResult(
                decision=SizerDecision.HALT,
                halt_reason=f"Invalid SL for short: SL={sl_price} <= Entry={entry_price}"
            )
        
        sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
        if sl_dist_pct < 0.01:
            # Слишком маленький стоп - это ошибка, а не нормальная ситуация
            return SizeResult(
                decision=SizerDecision.HALT,
                halt_reason=f"Stop loss too small: {sl_dist_pct:.4f}% < 0.01%"
            )

        # Проверка корректности плеча (используем локальную переменную)
        leverage = cfg.leverage
        if leverage < 1:
            leverage = 1

        risk_usd = portfolio.balance * risk_pct / 100
        position_usd = risk_usd / (sl_dist_pct / 100)  # leverage уже учтён на бирже
        units = position_usd / entry_price

        # Решение: REDUCE если любой из множителей сильно снижен
        is_reduced = (dd_mult < 0.75 or streak_mult < 0.75 or vol_mult < 0.75 or conf_mult < 0.75)
        decision = SizerDecision.REDUCE if is_reduced else SizerDecision.TRADE

        return SizeResult(
            decision=decision,
            risk_pct=risk_pct,
            position_usd=position_usd,
            units=units,
            base_risk_pct=base_risk,
            vol_multiplier=vol_mult,
            conf_multiplier=conf_mult,
            drawdown_multiplier=dd_mult,
            streak_multiplier=streak_mult,
        )

    # ---------- Вспомогательные методы ----------
    def _vol_multiplier(self, atr_pct: Optional[float]) -> float:
        if atr_pct is None or atr_pct <= 0:
            return 1.0
        ratio = self.cfg.normal_atr_pct / atr_pct
        mult = math.sqrt(ratio)  # смягчение
        return round(max(0.50, min(mult, 1.20)), 3)

    def _conf_multiplier(self, confidence: float) -> float:
        cfg = self.cfg
        # Проверка на равенство conf_max и conf_min
        if cfg.conf_max == cfg.conf_min:
            return 1.0
        conf = max(cfg.conf_min, min(confidence, cfg.conf_max))
        ratio = (conf - cfg.conf_min) / (cfg.conf_max - cfg.conf_min)
        mult = 0.60 + ratio * 0.40   # диапазон [0.60, 1.00]
        return round(mult, 3)

    def _drawdown_multiplier(self, drawdown_pct: float) -> float:
        cfg = self.cfg
        if drawdown_pct < cfg.drawdown_start_pct:
            return 1.0
        progress = (drawdown_pct - cfg.drawdown_start_pct) / (cfg.drawdown_halt_pct - cfg.drawdown_start_pct)
        mult = 1.0 - progress * 0.8   # до 0.20
        return round(max(0.20, mult), 3)

    def _streak_multiplier(self, consecutive_losses: int) -> float:
        cfg = self.cfg
        if consecutive_losses < cfg.consec_loss_start:
            return 1.0
        excess = consecutive_losses - cfg.consec_loss_start + 1
        mult = 1.0 / (1.0 + excess * 0.25)
        return round(max(0.25, mult), 3)

    def _kelly_risk(self, win_rate: float, avg_rr: float) -> float:
        full_kelly = win_rate - (1 - win_rate) / avg_rr
        fractional = full_kelly * self.cfg.kelly_fraction
        return round(max(self.cfg.min_risk_pct, min(fractional, self.cfg.max_risk_pct)), 3)