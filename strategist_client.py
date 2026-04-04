"""
strategist_client.py

Главный оркестратор торгового цикла.
"""

import asyncio
import logging
import uuid
from typing import List, Dict

from event_bus import EventBus, BotEvent, EventType
from signal_builder import SignalBuilder
from learning_engine import LearningEngine
from risk_manager import RiskManager, PortfolioState
from bitget_executor import BitgetExecutor, OrderRequest, OrderType
from performance_attribution import PerformanceAttribution, SignalSource
from telegram_bot import DinaBot

logger = logging.getLogger(__name__)


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
        self.direction = direction
        self.tiered_confidence_full = tiered_confidence_full
        self.tiered_confidence_half = tiered_confidence_half

        self._running = True
        self._paused = False
        self._active_orders: Dict[str, str] = {}  # symbol -> trade_id

        # Подписываемся на команды
        self.bus.subscribe(EventType.BOT_COMMAND, self._on_command)

        logger.info(f"StrategistClient initialized for {direction}")

    async def run_loop(self):
        """Главный цикл — опрос всех символов."""
        logger.info(f"StrategistClient {self.direction} started")
        while self._running:
            if not self._paused:
                for symbol in self.symbols:
                    try:
                        await self._process_symbol(symbol)
                    except Exception as e:
                        logger.error(f"Error processing {symbol}: {e}")
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(1)

    async def stop(self):
        self._running = False
        logger.info("StrategistClient stopped")

    async def _process_symbol(self, symbol: str):
        """Обрабатывает один символ."""
        signal = await self.signal_builder.compute(symbol, current_tf=self.timeframes[1])
        if "error" in signal:
            return

        composite = signal.get("composite_score", 0.0)
        if abs(composite) < 0.1:
            return

        if composite > 0:
            side = "long"
            raw_confidence = composite
        else:
            side = "short"
            raw_confidence = -composite

        # Корректировка confidence (пока без реальной логики)
        adjusted_confidence = raw_confidence
        if self.learning_engine and self.learning_engine._stats.total_trades > 0:
            # Здесь будет вызов adjust_confidence
            pass

        # Tiered confidence
        if adjusted_confidence >= self.tiered_confidence_full:
            size_pct = 5.0
        elif adjusted_confidence >= self.tiered_confidence_half:
            size_pct = 2.5
        else:
            logger.debug(f"{symbol}: confidence {adjusted_confidence:.2f} below threshold")
            return

        price = signal.get("price", 0.0)
        if price <= 0:
            return

        atr_pct = signal.get("atr_pct", 1.0)
        sl_pct = atr_pct * 1.5
        tp_pct = sl_pct * 2.0

        if side == "long":
            sl_price = price * (1 - sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100)
        else:
            sl_price = price * (1 + sl_pct / 100)
            tp_price = price * (1 - tp_pct / 100)

        risk_status = await self.risk_manager.check(
            portfolio=self.portfolio,
            symbol=symbol,
            entry_price=price,
            sl_price=sl_price,
            confidence=adjusted_confidence,
            atr_pct=atr_pct,
            direction=side,
        )

        if not risk_status.allowed:
            logger.info(f"{symbol}: RiskManager blocked - {risk_status.reason}")
            return

        size_usd = risk_status.size_result.position_usd

        trade_id = str(uuid.uuid4())[:8]
        req = OrderRequest(
            symbol=symbol,
            direction=side,
            size_usd=size_usd,
            entry_price=price,
            sl_price=sl_price,
            tp_price=tp_price,
            order_type=OrderType.MARKET,
            reason=f"signal composite={composite:.2f} conf={adjusted_confidence:.2f}",
            client_oid=f"dina_{trade_id}",
        )

        if self.bot:
            await self.bot.alert_signal(
                symbol=symbol,
                direction=side,
                entry_price=price,
                sl_price=sl_price,
                tp_price=tp_price,
                confidence=adjusted_confidence,
                reason=req.reason,
            )

        result = await self.executor.open_position(req)

        if result.success:
            self._active_orders[symbol] = trade_id

            sources = self._identify_sources(signal)
            sources.append(SignalSource.DEEPSEEK)
            if len(sources) > 1:
                sources.append(SignalSource.COMPOSITE)
            await self.attribution.record_open(
                trade_id=trade_id,
                symbol=symbol,
                direction=side,
                entry_price=price,
                sources=sources,
                deepseek_conf=adjusted_confidence,
            )

            if self.bot:
                await self.bot.alert_opened(
                    direction=side,
                    filled_price=result.filled_price,
                    size_usd=size_usd,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    dry_run=self.executor.cfg.dry_run,
                )

            self.risk_manager.on_trade_opened(symbol, size_usd, side, direction=self.direction)
            logger.info(f"{symbol}: position opened, trade_id={trade_id}")
        else:
            logger.error(f"{symbol}: failed to open position: {result.error}")

    def _identify_sources(self, signal: dict) -> List[SignalSource]:
        sources = []
        if signal.get("rsi") is not None:
            sources.append(SignalSource.TECHNICAL)
        if signal.get("fvg_bull") or signal.get("fvg_bear"):
            sources.append(SignalSource.TECHNICAL)
        if signal.get("sweep_bull") or signal.get("sweep_bear"):
            sources.append(SignalSource.TECHNICAL)
        return sources

    async def on_trade_closed(self, trade_id: str, symbol: str, exit_price: float, pnl_usd: float, pnl_pct: float, reason: str):
        """Вызывается из executor после закрытия позиции."""
        await self.attribution.record_close(trade_id, exit_price, pnl_pct, pnl_usd)
        self.portfolio.update(pnl_usd)
        self.risk_manager.on_trade_closed(pnl_usd)
        if self.bot:
            await self.bot.alert_closed(
                direction="",   # направление можно получить из attribution, но для упрощения
                entry_price=0,
                exit_price=exit_price,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                reason=reason,
                dry_run=self.executor.cfg.dry_run,
            )

    async def _on_command(self, event: BotEvent):
        cmd = event.data.get("command", "")
        if cmd == "stop":
            self._paused = True
            logger.info("StrategistClient paused by command")
        elif cmd == "start":
            self._paused = False
            logger.info("StrategistClient resumed by command")