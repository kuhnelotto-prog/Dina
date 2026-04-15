"""
strategist_client.py

Главный оркестратор торгового цикла.
Один экземпляр на направление (LONG или SHORT).
"""

import asyncio
import logging
import uuid
from typing import List, Dict, Optional

from event_bus import EventBus, BotEvent, EventType
from signal_builder import SignalBuilder
from learning_engine import LearningEngine
from risk_manager import RiskManager, PortfolioState, RiskStatus
from bitget_executor import BitgetExecutor, OrderRequest, OrderType
from performance_attribution import PerformanceAttribution, SignalSource
from telegram_bot import DinaBot

logger = logging.getLogger(__name__)

# Пороги входа — composite_score должен быть выше этого значения
# Динамические пороги зависят от рыночного режима BTC EMA50 на 4H:
#   BTC price > EMA50 → bullish: LONG агрессивнее, SHORT консервативнее
#   BTC price < EMA50 → bearish: LONG консервативнее, SHORT агрессивнее

# LONG пороги по режиму BTC:
ENTRY_THRESHOLD_LONG_BULL = 0.20      # bullish → агрессивнее (P3 tuned, was 0.30)
ENTRY_THRESHOLD_LONG_BEAR = 0.30      # bearish → консервативнее (P3 tuned, was 0.45)

# SHORT пороги по режиму BTC:
ENTRY_THRESHOLD_SHORT_BULL = 0.45     # bullish → строже на бычьем рынке (шорт против тренда)
ENTRY_THRESHOLD_SHORT_BEAR = 0.35     # bearish → агрессивнее на медвежьем (шорт по тренду)

# Funding rate: если |funding| > этого порога, повышаем threshold на FUNDING_PENALTY
FUNDING_EXTREME_THRESHOLD = 0.0005  # 0.05% за 8 часов = ~0.15%/день
FUNDING_PENALTY = 0.05              # +0.05 к порогу входа

# Trade cooldown: минимальное время между входами в один и тот же символ
COOLDOWN_SECONDS = 4 * 3600  # 4 часа


class StrategistClient:
    def __init__(
        self,
        bus: EventBus,
        symbols: List[str],
        timeframes: List[str],
        signal_builder: SignalBuilder,
        learning_engine: LearningEngine,
        attribution: PerformanceAttribution,
        risk_manager: RiskManager,
        portfolio: PortfolioState,
        executor: BitgetExecutor,
        bot: DinaBot,
        direction: str = "LONG",
        # Оставляем параметры для обратной совместимости, но не используем
        tiered_confidence_full: float = 0.75,
        tiered_confidence_half: float = 0.55,
    ):
        self.bus = bus
        self.symbols = symbols
        self.timeframes = timeframes
        self.signal_builder = signal_builder
        self.learning_engine = learning_engine
        self.attribution = attribution
        self.risk_manager = risk_manager
        self.portfolio = portfolio
        self.executor = executor
        self.bot = bot
        self.direction = direction.upper()  # "LONG" или "SHORT"

        # Для обратной совместимости (не используются в логике)
        self.tiered_confidence_full = tiered_confidence_full
        self.tiered_confidence_half = tiered_confidence_half

        self._running = True
        self._paused = False
        self._active_trades: Dict[str, str] = {}  # symbol -> trade_id
        self._last_trade_time: Dict[str, float] = {}  # symbol -> timestamp последнего входа

        # Подписываемся на команды
        self.bus.subscribe(EventType.BOT_COMMAND, self._on_command)

        logger.info(f"StrategistClient [{self.direction}] initialized | dynamic thresholds by BTC EMA50")

    # ──────────────────────────────────────────────
    # Главный цикл
    # ──────────────────────────────────────────────

    async def run_loop(self):
        """Главный цикл — опрос всех символов."""
        logger.info(f"StrategistClient [{self.direction}] started")
        while self._running:
            if not self._paused:
                for symbol in self.symbols:
                    try:
                        await self._process_symbol(symbol)
                    except Exception as e:
                        logger.error(f"[{self.direction}] Error processing {symbol}: {e}")
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(1)

    async def stop(self):
        self._running = False
        logger.info(f"StrategistClient [{self.direction}] stopped")

    # ──────────────────────────────────────────────
    # Обработка одного символа
    # ──────────────────────────────────────────────

    async def _process_symbol(self, symbol: str):
        """Обрабатывает один символ."""
        # ── Blacklist ──
        from backtester import ADX_BLACKLIST, ADXFilter
        if symbol in ADX_BLACKLIST:
            return

        signal = await self.signal_builder.compute(symbol, current_tf=self.timeframes[1])
        if "error" in signal:
            return

        # ── ADX Filter (BEFORE Score) ──
        adx_val = signal.get("adx", 0.0)
        adx_prev = signal.get("adx_prev", 0.0)
        _adx_filter = ADXFilter(threshold=18.0)  # P3: adx_growth removed
        adx_ok, adx_reason = _adx_filter.check(adx_val, adx_prev)
        if not adx_ok:
            logger.debug(f"[{self.direction}] {symbol}: ADX rejected: {adx_reason}")
            return

        composite = signal.get("composite_score", 0.0)

        # ── Disabled sources filter (P1.5) ──
        disabled = self.learning_engine.disabled_sources
        if disabled:
            for src in disabled:
                logger.info(f"[{self.direction}] {symbol}: Source '{src}' disabled, ignoring in composite")
            # Если ВСЕ источники disabled — пропускаем символ
            from learning_engine import DEFAULT_WEIGHTS
            if len(disabled) >= len(DEFAULT_WEIGHTS):
                logger.warning(f"[{self.direction}] {symbol}: ALL sources disabled, skipping")
                return

        # ── Funding rate коррекция порога ──
        # Положительный funding = лонги платят шортам → штраф для LONG, бонус для SHORT
        # Отрицательный funding = шорты платят лонгам → штраф для SHORT, бонус для LONG
        funding_penalty = 0.0
        try:
            funding_rate = await self.executor.get_funding_rate(symbol)
            if funding_rate > FUNDING_EXTREME_THRESHOLD:
                if self.direction == "LONG":
                    funding_penalty = FUNDING_PENALTY   # лонг платит — штраф
                else:
                    funding_penalty = -FUNDING_PENALTY  # шорт получает — бонус
                logger.info(
                    f"[{self.direction}] {symbol}: positive funding={funding_rate:.6f}, "
                    f"threshold {'+' if funding_penalty > 0 else ''}{funding_penalty:.2f}"
                )
            elif funding_rate < -FUNDING_EXTREME_THRESHOLD:
                if self.direction == "SHORT":
                    funding_penalty = FUNDING_PENALTY   # шорт платит — штраф
                else:
                    funding_penalty = -FUNDING_PENALTY  # лонг получает — бонус
                logger.info(
                    f"[{self.direction}] {symbol}: negative funding={funding_rate:.6f}, "
                    f"threshold {'+' if funding_penalty > 0 else ''}{funding_penalty:.2f}"
                )
        except Exception as e:
            logger.debug(f"[{self.direction}] {symbol}: funding rate unavailable: {e}")

        # ── Direction фильтр ──
        # Динамический порог по BTC EMA50 на 4H:
        #   BULL: LONG агрессивнее (0.30), SHORT консервативнее (0.45)
        #   BEAR: LONG консервативнее (0.45), SHORT агрессивнее (0.30)
        btc_regime = self.signal_builder.detect_btc_regime()

        if self.direction == "LONG":
            base_threshold = ENTRY_THRESHOLD_LONG_BULL if btc_regime == "BULL" else ENTRY_THRESHOLD_LONG_BEAR
            threshold = base_threshold + funding_penalty
            if composite <= threshold:
                return
            side = "long"
            confidence = composite
            logger.info(
                f"[LONG] {symbol}: btc_regime={btc_regime} threshold={threshold:.2f} "
                f"composite={composite:.3f}"
            )
        elif self.direction == "SHORT":
            base_threshold = ENTRY_THRESHOLD_SHORT_BEAR if btc_regime == "BEAR" else ENTRY_THRESHOLD_SHORT_BULL
            threshold = base_threshold + funding_penalty
            if composite >= -threshold:
                return
            side = "short"
            confidence = abs(composite)
            logger.info(
                f"[SHORT] {symbol}: btc_regime={btc_regime} threshold={threshold:.2f} "
                f"composite={composite:.3f}"
            )
        else:
            return

        price = signal.get("price", 0.0)
        if price <= 0:
            return

        # ── Trade Cooldown ──
        # Проверяем ТОЛЬКО при открытии новой позиции.
        # Trailing/stage/partial close не затрагивают cooldown.
        import time as _time
        last_trade = self._last_trade_time.get(symbol, 0)
        elapsed = _time.time() - last_trade
        if elapsed < COOLDOWN_SECONDS:
            remaining_min = (COOLDOWN_SECONDS - elapsed) / 60
            logger.debug(
                f"[{self.direction}] {symbol}: cooldown active, "
                f"{remaining_min:.0f} min remaining"
            )
            return

        # ── SL/TP по ATR ──
        atr_pct = signal.get("atr_pct", 1.0)
        sl_pct = atr_pct * 1.5
        tp_pct = atr_pct * 2.0  # TP привязан к ATR напрямую, синхронизирован с трейлинг-этапом 4 (2.0 ATR)

        if side == "long":
            sl_price = price * (1 - sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100)
        else:
            sl_price = price * (1 + sl_pct / 100)
            tp_price = price * (1 - tp_pct / 100)

        # ── RiskManager check ──
        risk_status: RiskStatus = await self.risk_manager.check(
            portfolio=self.portfolio,
            symbol=symbol,
            entry_price=price,
            sl_price=sl_price,
            confidence=confidence,
            atr_pct=atr_pct,
            direction=side,
        )

        if not risk_status.allowed:
            logger.info(f"[{self.direction}] {symbol}: RiskManager blocked — {risk_status.reason}")
            return

        if risk_status.size_result is None:
            logger.error(f"[{self.direction}] {symbol}: RiskManager allowed but size_result is None")
            return

        size_usd = risk_status.size_result.position_usd

        # ── Создаём ордер ──
        trade_id = str(uuid.uuid4())[:8]
        req = OrderRequest(
            symbol=symbol,
            direction=side,
            size_usd=size_usd,
            entry_price=price,
            sl_price=sl_price,
            tp_price=tp_price,
            order_type=OrderType.MARKET,
            reason=f"{self.direction} composite={composite:.3f}",
            client_oid=f"dina_{trade_id}",
        )

        # Telegram alert
        if self.bot:
            await self.bot.alert_signal(
                symbol=symbol,
                direction=side,
                entry_price=price,
                sl_price=sl_price,
                tp_price=tp_price,
                confidence=confidence,
                reason=req.reason,
            )

        # ── Исполнение ──
        result = await self.executor.open_position(req)

        if result.success:
            self._active_trades[symbol] = trade_id

            # Attribution
            sources = self._identify_sources(signal)
            setup_type = self._determine_setup_type(signal)
            await self.attribution.record_open(
                trade_id=trade_id,
                symbol=symbol,
                direction=side,
                entry_price=price,
                sources=sources,
                deepseek_conf=confidence,
                setup_type=setup_type,
            )

            # Telegram
            if self.bot:
                await self.bot.alert_opened(
                    symbol=symbol,
                    direction=side,
                    filled_price=result.filled_price,
                    size_usd=size_usd,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    dry_run=self.executor.cfg.dry_run,
                )

            # RiskManager
            self.risk_manager.on_trade_opened(symbol, size_usd, side, direction=self.direction)

            # Cooldown: записываем время входа
            self._last_trade_time[symbol] = _time.time()

            logger.info(f"[{self.direction}] {symbol}: position opened | trade_id={trade_id} | size=${size_usd:.0f}")
        else:
            logger.error(f"[{self.direction}] {symbol}: failed to open — {result.error}")

    # ──────────────────────────────────────────────
    # Закрытие позиции (вызывается извне)
    # ──────────────────────────────────────────────

    async def on_trade_closed(
        self,
        trade_id: str,
        symbol: str,
        exit_price: float,
        pnl_usd: float,
        pnl_pct: float,
        reason: str,
    ):
        """
        Вызывается из orchestrator/monitor при закрытии позиции.
        Обновляет portfolio, risk_manager, attribution.
        """
        # Attribution
        await self.attribution.record_close(trade_id, exit_price, pnl_pct, pnl_usd)

        # Portfolio state
        self.portfolio.update(pnl_usd)

        # RiskManager
        self.risk_manager.on_trade_closed(pnl_usd, symbol)

        # Убираем из активных
        self._active_trades.pop(symbol, None)

        # Telegram
        if self.bot:
            await self.bot.alert_closed(
                symbol=symbol,
                direction=self.direction.lower(),
                entry_price=0,
                exit_price=exit_price,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                reason=reason,
                dry_run=self.executor.cfg.dry_run,
            )

        logger.info(
            f"[{self.direction}] {symbol}: closed | PnL: {pnl_usd:+.2f}$ ({pnl_pct:+.2f}%) | reason: {reason}"
        )

    # ──────────────────────────────────────────────
    # Вспомогательные
    # ──────────────────────────────────────────────

    def _identify_sources(self, signal: dict) -> List[SignalSource]:
        """Определяет источники сигнала для attribution."""
        sources = [SignalSource.TECHNICAL]
        if signal.get("fvg_bull") or signal.get("fvg_bear"):
            sources.append(SignalSource.TECHNICAL)
        if signal.get("sweep_bull") or signal.get("sweep_bear"):
            sources.append(SignalSource.TECHNICAL)
        return sources

    def _determine_setup_type(self, signal: dict) -> str:
        """
        Определяет тип сетапа на основе сигналов.
        
        Возможные значения:
          - trend_continuation: EMA cross + ADX trending
          - trend_reversal: engulfing + FVG или divergence
          - breakout: breakout сигнал (Bollinger/Keltner)
          - fvg: Fair Value Gap доминирует
          - sweep: Liquidity sweep доминирует
          - unknown: не удалось определить
        """
        has_fvg = signal.get("fvg_bull") or signal.get("fvg_bear")
        has_sweep = signal.get("sweep_bull") or signal.get("sweep_bear")
        has_engulfing = signal.get("engulfing_bull") or signal.get("engulfing_bear")
        has_ema_cross = signal.get("ema_cross_bull") or signal.get("ema_cross_bear")
        has_breakout = signal.get("bb_breakout") or signal.get("keltner_breakout")
        has_divergence = signal.get("rsi_divergence") or signal.get("macd_divergence")
        adx_trending = signal.get("adx", 0) > 25

        # Приоритет определения:
        # 1. sweep (ликвидность) — если есть sweep
        if has_sweep:
            return "sweep"
        
        # 2. fvg — если FVG доминирует
        if has_fvg and not has_ema_cross:
            return "fvg"
        
        # 3. breakout — если есть breakout сигнал
        if has_breakout:
            return "breakout"
        
        # 4. trend_reversal — engulfing + (FVG или divergence)
        if has_engulfing and (has_fvg or has_divergence):
            return "trend_reversal"
        
        # 5. trend_continuation — EMA cross + ADX trending
        if has_ema_cross and adx_trending:
            return "trend_continuation"
        
        # 6. trend_continuation fallback — EMA cross без ADX
        if has_ema_cross:
            return "trend_continuation"
        
        return "unknown"

    async def _on_command(self, event: BotEvent):
        """Обработка команд из Telegram."""
        cmd = event.data.get("command", "")
        if cmd == "stop":
            self._paused = True
            logger.info(f"StrategistClient [{self.direction}] paused by command")
        elif cmd == "start":
            self._paused = False
            logger.info(f"StrategistClient [{self.direction}] resumed by command")