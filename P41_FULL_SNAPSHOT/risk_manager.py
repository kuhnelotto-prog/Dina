"""
risk_manager.py

Финальный страж перед исполнением.
Проверяет:
  - Дневной лимит потерь
  - Лимит открытых позиций
  - Общую экспозицию по всем символам
  - Корреляцию между позициями
  - Просадку и серию потерь (через PositionSizer)
  - Аварийные стопы
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Tuple

import numpy as np
import pandas as pd

from position_sizer import PositionSizer, PortfolioState, SizerConfig, SizerDecision, SizeResult
from market_regime import MarketRegimeDetector, MarketRegime

logger = logging.getLogger(__name__)


class DrawdownState(str, Enum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    EMERGENCY = "EMERGENCY"


@dataclass
class RiskStatus:
    allowed: bool
    state: DrawdownState
    reason: str = ""
    size_result: Optional[SizeResult] = None

    @property
    def risk_status_str(self) -> str:
        return "OK" if self.allowed else "BLOCKED"

    @property
    def drawdown_state_str(self) -> str:
        return self.state.value


class RiskManager:
    def __init__(
        self,
        sizer_config: Optional[SizerConfig] = None,
        max_open_positions: int = 1,
        daily_loss_limit: float = 5.0,
        max_total_exposure_usd: float = 5000.0,
        learning_engine=None,
    ):
        self.sizer = PositionSizer(sizer_config or SizerConfig())
        self.learning_engine = learning_engine  # Optional[LearningEngine]
        self.max_open_positions = max_open_positions
        self.daily_loss_limit = daily_loss_limit
        self.max_total_exposure_usd = max_total_exposure_usd

        self._open_positions: Dict[str, dict] = {}          # symbol -> position info
        self._open_count: int = 0
        self._daily_pnl: float = 0.0
        self._day_start: datetime = datetime.now(timezone.utc)

        # Кэш свечей 4H для расчёта корреляции секторов
        # (symbol, "4h") -> pd.DataFrame с колонкой "close"
        self._candle_cache: Dict[str, pd.DataFrame] = {}

        # Секторные группы (единый источник правды для всех проверок)
        # Обновлено после бэктестов: оставлены только прибыльные монеты
        self.SECTOR_GROUPS = {
            "L1": ["BTCUSDT", "ETHUSDT"],
            "L2": ["BNBUSDT"],              # BNB Chain L2
            "DeFi": ["LINKUSDT"],
            "AI": [],                       # зарезервировано
            "Gaming": [],                   # зарезервировано
            "Infra": ["AVAXUSDT"],          # Avalanche Subnets
            "Meme": ["DOGEUSDT"],
            "Alt_L1": ["XRPUSDT", "SOLUSDT", "ADAUSDT", "SUIUSDT"],
        }

        # Лимиты позиций по секторам
        self.SECTOR_LIMITS = {
            "L1": 2,        # макс 2 позиции в L1
            "Alt_L1": 2,    # макс 2 позиции в Alt_L1 (XRP, SOL, ADA, SUI)
            "default": 1,   # макс 1 позиция в остальных секторах
        }

        # Кэш корреляций секторов (обновляется раз в цикл)
        self._sector_corr_cache: Dict[str, float] = {}
        self._corr_cache_ts: float = 0.0
        self._corr_cache_ttl: float = 3600.0  # обновлять раз в час

        # MarketRegimeDetector
        self.regime_detector = MarketRegimeDetector()

        logger.info(f"RiskManager init | max_pos={max_open_positions} | daily_loss_limit={daily_loss_limit}% | total_exposure_limit=${max_total_exposure_usd}")

    # ============================================================
    # Основная проверка перед входом
    # ============================================================

    async def check(
        self,
        portfolio: PortfolioState,
        symbol: str,
        entry_price: float,
        sl_price: float,
        confidence: float,
        atr_pct: Optional[float] = None,
        win_rate: Optional[float] = None,
        avg_rr: Optional[float] = None,
        direction: str = "long",
    ) -> RiskStatus:
        # Сброс дневного счётчика
        self._reset_daily_if_needed()

        # 1. Лимит открытых позиций
        if self._open_count >= self.max_open_positions:
            return RiskStatus(
                allowed=False,
                state=DrawdownState.NORMAL,
                reason=f"Already {self._open_count} positions (limit {self.max_open_positions})"
            )

        # 2. Дневной лимит потерь
        daily_loss_pct = self._daily_pnl / portfolio.balance * 100 if portfolio.balance else 0
        if daily_loss_pct <= -self.daily_loss_limit:
            return RiskStatus(
                allowed=False,
                state=DrawdownState.EMERGENCY,
                reason=f"Daily loss limit reached: {daily_loss_pct:.1f}% ≤ -{self.daily_loss_limit}%"
            )

        # 3. Общая экспозиция (сумма size_usd всех открытых позиций)
        total_exposure = sum(p.get("size_usd", 0) for p in self._open_positions.values())
        # Оценка размера новой позиции: risk_usd / sl_distance * leverage
        sl_dist_pct = abs(entry_price - sl_price) / entry_price if entry_price > 0 else 0.01
        estimated_position_usd = (portfolio.balance * self.sizer.cfg.base_risk_pct / 100) / max(sl_dist_pct, 0.001)
        if total_exposure + estimated_position_usd > self.max_total_exposure_usd:
            return RiskStatus(
                allowed=False,
                state=DrawdownState.NORMAL,
                reason=f"Total exposure would exceed ${self.max_total_exposure_usd}"
            )

        # 4. Корреляция с существующими позициями (группы L1, DeFi, Meme + BTC)
        if not self._check_correlation(symbol, direction):
            return RiskStatus(
                allowed=False,
                state=DrawdownState.NORMAL,
                reason="Correlation limit exceeded (same sector already open)"
            )

        # 4.5. Проверка рыночного режима (MarketRegimeDetector)
        btc_regime = self.regime_detector.detect("BTCUSDT")
        symbol_regime = self.regime_detector.detect(symbol) if symbol != "BTCUSDT" else btc_regime

        if btc_regime == MarketRegime.CRISIS:
            return RiskStatus(
                allowed=False,
                state=DrawdownState.EMERGENCY,
                reason=f"🛑 Market in CRISIS mode (BTC BEAR + ATR > 2× avg)"
            )

        # Флаг для снижения размера при VOLATILE
        volatile_multiplier = 1.0
        if btc_regime == MarketRegime.VOLATILE or symbol_regime == MarketRegime.VOLATILE:
            volatile_multiplier = 0.7
            logger.info(
                f"RiskManager: VOLATILE regime detected (BTC={btc_regime.value}, "
                f"{symbol}={symbol_regime.value}) → size ×0.7"
            )

        # 5. Расчёт размера через PositionSizer (учитывает просадку, серию, vol, conf)
        size_result = self.sizer.calculate(
            portfolio=portfolio,
            entry_price=entry_price,
            sl_price=sl_price,
            confidence=confidence,
            atr_pct=atr_pct,
            win_rate=win_rate,
            avg_rr=avg_rr,
            side=direction,
        )

        if size_result.decision == SizerDecision.HALT:
            state = DrawdownState.EMERGENCY if portfolio.drawdown_pct >= self.sizer.cfg.drawdown_halt_pct else DrawdownState.DEGRADED

            # 3.3: Emergency reset weights on HALT
            if self.learning_engine is not None:
                dd = portfolio.drawdown_pct
                cl = portfolio.consecutive_losses
                self.learning_engine.reset_to_defaults(
                    f"HALT: dd={dd:.1f}% losses={cl}"
                )

            return RiskStatus(
                allowed=False,
                state=state,
                reason=size_result.halt_reason,
                size_result=size_result,
            )

        state = DrawdownState.DEGRADED if size_result.decision == SizerDecision.REDUCE else DrawdownState.NORMAL

        # 6. Корреляционный фильтр для шортов
        if direction.lower() == "short":
            corr_allowed, corr_mult, corr_reason = self._check_sector_correlation_for_short(symbol)
            if not corr_allowed:
                logger.info(f"RiskManager: {corr_reason}")
                return RiskStatus(
                    allowed=False,
                    state=DrawdownState.DEGRADED,
                    reason=corr_reason,
                    size_result=size_result,
                )
            if corr_mult < 1.0:
                old_size = size_result.position_usd
                size_result.position_usd = old_size * corr_mult
                logger.info(f"RiskManager: {corr_reason} | size ${old_size:.0f} → ${size_result.position_usd:.0f}")

        # 6.5. Применяем volatile_multiplier если рынок волатильный
        if volatile_multiplier < 1.0:
            old_size = size_result.position_usd
            size_result.position_usd = old_size * volatile_multiplier
            logger.info(
                f"RiskManager: volatile adjustment ${old_size:.0f} → ${size_result.position_usd:.0f} "
                f"(×{volatile_multiplier})"
            )

        # 7. VaR-проверка
        var_ok, var_usd = self.check_var_limit(portfolio)
        if not var_ok:
            self.apply_var_reduction()
            var_msg = f"VaR limit exceeded: ${var_usd:.0f} > {portfolio.balance * 0.10:.0f} (10% of balance)"
            return RiskStatus(
                allowed=False,
                state=DrawdownState.DEGRADED,
                reason=var_msg,
                size_result=size_result,
            )

        # 8. Всё прошло
        return RiskStatus(
            allowed=True,
            state=state,
            reason=f"Position size: ${size_result.position_usd:,.0f} (risk {size_result.risk_pct:.2f}%)",
            size_result=size_result,
        )

    # ============================================================
    # Управление открытыми позициями
    # ============================================================

    def on_trade_opened(self, symbol: str, size_usd: float, side: str, direction: str = "long"):
        self._open_positions[symbol] = {
            "symbol": symbol,
            "side": side,
            "direction": direction,
            "size_usd": size_usd,
            "opened_at": time.time(),
        }
        self._open_count += 1

    def update_position_size(self, symbol: str, remaining_pct: float):
        """
        Обновляет размер позиции после partial close.
        remaining_pct: доля оставшейся позиции (0.75 после 25% close, 0.50 после 50%).
        """
        pos = self._open_positions.get(symbol)
        if pos is None:
            logger.warning(f"RiskManager: update_position_size — {symbol} not found")
            return
        old_size = pos["size_usd"]
        pos["size_usd"] = old_size * remaining_pct
        logger.info(
            f"RiskManager: {symbol} size updated: ${old_size:.0f} → ${pos['size_usd']:.0f} "
            f"(remaining {remaining_pct*100:.0f}%)"
        )

    def get_position_size(self, symbol: str) -> float:
        """Возвращает текущий размер позиции (с учётом partial close)."""
        pos = self._open_positions.get(symbol)
        return pos["size_usd"] if pos else 0.0

    def on_trade_closed(self, pnl_usd: float, symbol: Optional[str] = None):
        self._daily_pnl += pnl_usd
        self._open_count = max(0, self._open_count - 1)
        if symbol is not None:
            self._open_positions.pop(symbol, None)

    # ============================================================
    # Корреляция секторов (4H свечи)
    # ============================================================

    def update_candles(self, symbol: str, df: pd.DataFrame):
        """
        Обновляет кэш свечей для расчёта корреляции.
        Вызывается из DataFeed при получении 4H свечей.
        """
        self._candle_cache[symbol] = df

    def _get_symbol_sector(self, symbol: str) -> Optional[str]:
        """Возвращает сектор символа или None."""
        for sector, symbols in self.SECTOR_GROUPS.items():
            if symbol in symbols:
                return sector
        return None

    def _calculate_sector_correlation(self, sector: str, window: int = 20) -> float:
        """
        Средняя попарная корреляция returns между активами сектора.
        window: количество 4H свечей (20 свечей ≈ 3.3 дня, но 120 = 20 дней).
        Используем 120 свечей = 20 дней × 6 свечей/день.
        
        Returns: средняя корреляция (0..1), или 0.0 если данных недостаточно.
        """
        candle_window = window * 6  # 20 дней × 6 свечей (4H) = 120 свечей
        symbols = self.SECTOR_GROUPS.get(sector, [])
        
        # Собираем returns для каждого символа
        returns_dict = {}
        for sym in symbols:
            df = self._candle_cache.get(sym)
            if df is None or len(df) < candle_window:
                continue
            close = df["close"] if "close" in df.columns else df.iloc[:, 4]
            close = close.tail(candle_window).astype(float)
            ret = close.pct_change().dropna()
            if len(ret) >= candle_window - 1:
                returns_dict[sym] = ret.values

        if len(returns_dict) < 2:
            return 0.0  # недостаточно данных

        # Строим матрицу корреляций
        symbols_list = list(returns_dict.keys())
        n = len(symbols_list)
        
        # Выравниваем длины
        min_len = min(len(v) for v in returns_dict.values())
        matrix = np.array([returns_dict[s][-min_len:] for s in symbols_list])
        
        # Корреляционная матрица
        corr_matrix = np.corrcoef(matrix)
        
        # Средняя попарная корреляция (верхний треугольник без диагонали)
        pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                val = corr_matrix[i, j]
                if not np.isnan(val):
                    pairs.append(abs(val))

        return float(np.mean(pairs)) if pairs else 0.0

    def _get_all_sector_correlations(self) -> Dict[str, float]:
        """
        Возвращает корреляции всех секторов (с кэшированием на 1 час).
        """
        now = time.time()
        if now - self._corr_cache_ts < self._corr_cache_ttl and self._sector_corr_cache:
            return self._sector_corr_cache

        result = {}
        for sector in self.SECTOR_GROUPS:
            corr = self._calculate_sector_correlation(sector)
            result[sector] = corr
            if corr > 0.5:
                logger.info(f"📊 Sector {sector} correlation: {corr:.3f}")

        self._sector_corr_cache = result
        self._corr_cache_ts = now
        return result

    def _check_sector_correlation_for_short(self, symbol: str) -> Tuple[bool, float, str]:
        """
        Проверяет корреляцию сектора для шорт-сделки.
        
        Returns:
            (allowed, size_multiplier, reason)
            - allowed=False если corr > 0.85 в 2+ секторах
            - size_multiplier=0.7 если corr > 0.75 в секторе символа
            - size_multiplier=1.0 если всё ок
        """
        sector_corrs = self._get_all_sector_correlations()
        
        # Проверка 1: если corr > 0.85 в 2+ секторах → блокируем шорт
        high_corr_sectors = [s for s, c in sector_corrs.items() if c > 0.85]
        if len(high_corr_sectors) >= 2 and self._open_count > 0:
            return (
                False, 0.0,
                f"🚫 SHORT blocked: high correlation (>0.85) in {len(high_corr_sectors)} sectors: "
                f"{', '.join(f'{s}={sector_corrs[s]:.2f}' for s in high_corr_sectors)}"
            )

        # Проверка 2: если corr > 0.75 в секторе символа → уменьшаем размер на 30%
        symbol_sector = self._get_symbol_sector(symbol)
        if symbol_sector and sector_corrs.get(symbol_sector, 0) > 0.75:
            corr_val = sector_corrs[symbol_sector]
            return (
                True, 0.7,
                f"⚠️ SHORT size reduced 30%: {symbol_sector} correlation={corr_val:.2f} > 0.75"
            )

        return (True, 1.0, "")

    # ============================================================
    # Portfolio VaR (Value at Risk)
    # ============================================================

    def calculate_portfolio_var(self, atr_pct_by_symbol: Optional[Dict[str, float]] = None) -> float:
        """
        Упрощённый портфельный VaR (95% confidence).
        
        Формула: VaR = total_exposure × avg_volatility × z_score
        
        Args:
            atr_pct_by_symbol: словарь {symbol: atr_pct} для каждой открытой позиции.
                              Если None — используем дефолт 1.5%.
        Returns:
            VaR в USD (максимальный ожидаемый убыток за 1 день с 95% вероятностью)
        """
        z_score = 1.65  # 95% confidence

        if not self._open_positions:
            return 0.0

        total_exposure = 0.0
        weighted_vol = 0.0

        for symbol, pos in self._open_positions.items():
            size_usd = pos.get("size_usd", 0)
            total_exposure += size_usd

            # ATR% как proxy для дневной волатильности
            if atr_pct_by_symbol and symbol in atr_pct_by_symbol:
                vol = atr_pct_by_symbol[symbol] / 100.0
            else:
                vol = 0.015  # дефолт 1.5%

            weighted_vol += size_usd * vol

        if total_exposure <= 0:
            return 0.0

        avg_volatility = weighted_vol / total_exposure
        var_usd = total_exposure * avg_volatility * z_score

        return var_usd

    def check_var_limit(
        self,
        portfolio: PortfolioState,
        atr_pct_by_symbol: Optional[Dict[str, float]] = None,
        var_limit_pct: float = 0.10,
    ) -> Tuple[bool, float]:
        """
        Проверяет VaR лимит и возвращает (exceeded, var_usd).
        
        Args:
            portfolio: текущее состояние портфеля
            atr_pct_by_symbol: ATR% по символам
            var_limit_pct: максимальный VaR как доля от баланса (0.10 = 10%)
            
        Returns:
            (exceeded: bool, var_usd: float)
        """
        var_usd = self.calculate_portfolio_var(atr_pct_by_symbol)
        var_limit_usd = portfolio.balance * var_limit_pct
        exceeded = var_usd > var_limit_usd

        if exceeded:
            logger.warning(
                f"⚠️ VaR limit exceeded: ${var_usd:.0f} > ${var_limit_usd:.0f} "
                f"({var_usd/portfolio.balance*100:.1f}% > {var_limit_pct*100:.0f}%)"
            )

        return exceeded, var_usd

    def apply_var_reduction(self):
        """Уменьшает max_risk_pct вдвое при превышении VaR."""
        if not hasattr(self, '_original_max_risk_pct'):
            self._original_max_risk_pct = self.sizer.cfg.max_risk_pct
        
        new_risk = self._original_max_risk_pct * 0.5
        self.sizer.cfg.max_risk_pct = new_risk
        logger.warning(f"VaR limit exceeded, reducing max_risk_pct to {new_risk:.2f}%")

    def restore_var_risk(self):
        """Восстанавливает исходный max_risk_pct после снятия VaR лимита."""
        if hasattr(self, '_original_max_risk_pct'):
            self.sizer.cfg.max_risk_pct = self._original_max_risk_pct
            logger.info(f"VaR limit cleared, restoring max_risk_pct to {self._original_max_risk_pct:.2f}%")

    # ============================================================
    # Внутренние методы
    # ============================================================

    def _reset_daily_if_needed(self):
        now = datetime.now(timezone.utc)
        if now.date() > self._day_start.date():
            logger.info(f"RiskManager: reset daily PnL (was {self._daily_pnl:+.2f}$)")
            self._daily_pnl = 0.0
            self._day_start = now

    def _check_correlation(self, symbol: str, direction: str) -> bool:
        """
        Секторная корреляция: ограничивает количество позиций в одном секторе.
        
        Лимиты:
          - L1 (BTC, ETH): макс 2 позиции
          - Остальные секторы: макс 1 позиция
          
        BTC-коэффициент: BTCUSDT занимает 0.5 слота в L1
        (т.к. BTC менее волатилен и менее коррелирован с ETH на коротких таймфреймах)
        
        # TODO: в будущем ограничивать по общей экспозиции в секторе, а не по количеству позиций
        """
        symbol_group = self._get_symbol_sector(symbol)

        if not symbol_group:
            return True  # символ не в секторе — пропускаем

        # Считаем сколько позиций уже открыто в этом секторе
        # ТОЛЬКО позиции того же направления (LONG vs LONG, SHORT vs SHORT)
        # Противоположные направления = хедж, не корреляция
        sector_slots_used = 0.0
        for pos in self._open_positions.values():
            pos_group = self._get_symbol_sector(pos["symbol"])
            if pos_group == symbol_group:
                # Учитываем только позиции того же направления
                pos_direction = pos.get("direction", pos.get("side", "long")).lower()
                if pos_direction != direction.lower():
                    continue  # противоположное направление = хедж, не считаем
                # BTC занимает 0.5 слота в L1
                if pos["symbol"] == "BTCUSDT" and symbol_group == "L1":
                    sector_slots_used += 0.5
                else:
                    sector_slots_used += 1.0

        # Сколько слотов займёт новая позиция
        new_slot = 0.5 if (symbol == "BTCUSDT" and symbol_group == "L1") else 1.0

        # Лимит для сектора
        sector_limit = self.SECTOR_LIMITS.get(symbol_group, self.SECTOR_LIMITS["default"])

        if sector_slots_used + new_slot > sector_limit:
            logger.info(
                f"RiskManager: {symbol} blocked — sector {symbol_group} "
                f"slots={sector_slots_used:.1f}+{new_slot:.1f} > limit={sector_limit}"
            )
            return False

        return True

    # ============================================================
    # Статус для Telegram
    # ============================================================

    def status_str(self, portfolio: PortfolioState) -> str:
        dd = portfolio.drawdown_pct
        daily_pct = self._daily_pnl / portfolio.balance * 100 if portfolio.balance else 0

        if dd >= self.sizer.cfg.drawdown_halt_pct:
            state = DrawdownState.EMERGENCY
        elif dd >= self.sizer.cfg.drawdown_start_pct:
            state = DrawdownState.DEGRADED
        else:
            state = DrawdownState.NORMAL

        state_emoji = {
            DrawdownState.NORMAL: "✅",
            DrawdownState.DEGRADED: "⚠️",
            DrawdownState.EMERGENCY: "🛑",
        }

        total_exposure = sum(p.get("size_usd", 0) for p in self._open_positions.values())

        return (
            f"{state_emoji[state]} RiskManager\n"
            f"Drawdown:    -{dd:.1f}% / limit -{self.sizer.cfg.drawdown_halt_pct:.0f}%\n"
            f"Дневной PnL: {daily_pct:+.1f}% / limit -{self.daily_loss_limit:.0f}%\n"
            f"Открыто:     {self._open_count}/{self.max_open_positions}\n"
            f"Экспозиция:  ${total_exposure:,.0f} / ${self.max_total_exposure_usd:,.0f}\n"
            f"Серия потерь: {portfolio.consecutive_losses}"
        )