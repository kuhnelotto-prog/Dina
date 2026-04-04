"""
telegram_bot.py

Telegram-интерфейс для Дины.

Команды:
  /start, /status, /history, /pnl, /pause, /resume, /close, /setlimit, /risk, /attribution

Алерты (автоматически):
  — Signal detected, Position opened / closed, SL hit / TP hit
  — Drawdown warning, Emergency halt, Critical errors

Поддержка ночного режима и приоритетов.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)


# ============================================================
# Конфиг
# ============================================================

@dataclass
class TelegramConfig:
    token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    allowed_ids: set = field(default_factory=lambda: {int(x) for x in os.getenv("TELEGRAM_ALLOWED_IDS", "").split(",") if x})
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "dina.db"))

    # Ночной режим (UTC)
    silent_start_hour: int = 23
    silent_end_hour: int = 7


# ============================================================
# Состояние бота
# ============================================================

class BotState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"


# ============================================================
# DinaBot
# ============================================================

class DinaBot:
    def __init__(
        self,
        config: TelegramConfig = None,
        strategist=None,
        risk_manager=None,
        portfolio=None,
        executor=None,
        attribution=None,
        symbols=None,
        main_loop: asyncio.AbstractEventLoop = None,
    ):
        self.cfg = config or TelegramConfig()
        self.strategist = strategist
        self.risk_manager = risk_manager
        self.portfolio = portfolio
        self.executor = executor
        self.attribution = attribution
        self.symbols = symbols or []
        self.state = BotState.RUNNING
        self._app: Optional[Application] = None
        self._owner_chat_id: Optional[int] = None
        self._main_loop = main_loop
        self._stop_event: Optional[asyncio.Event] = None
        self._tg_loop = None

        # Буфер для ночных сообщений
        self._night_buffer: List[dict] = []
        self._night_mode = False

        if self.cfg.allowed_ids:
            self._owner_chat_id = next(iter(self.cfg.allowed_ids))

    # ============================================================
    # Инициализация
    # ============================================================

    async def setup(self):
        if not self.cfg.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

        self._app = Application.builder().token(self.cfg.token).build()

        handlers = [
            ("start", self._cmd_start),
            ("status", self._cmd_status),
            ("history", self._cmd_history),
            ("pnl", self._cmd_pnl),
            ("pause", self._cmd_pause),
            ("resume", self._cmd_resume),
            ("close", self._cmd_close),
            ("setlimit", self._cmd_setlimit),
            ("risk", self._cmd_risk),
            ("attribution", self._cmd_attribution),
        ]
        for name, fn in handlers:
            self._app.add_handler(CommandHandler(name, fn))

        self._app.add_handler(CallbackQueryHandler(self._on_callback))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

        logger.info("DinaBot: handlers registered")

    async def run(self):
        self._stop_event = asyncio.Event()
        await self.setup()
        logger.info("DinaBot: starting polling...")
        
        # Запускаем polling напрямую - он сам управляет своим event loop
        try:
            await self._app.run_polling(drop_pending_updates=True)
        except asyncio.CancelledError:
            logger.info("DinaBot: polling cancelled")
        except Exception as e:
            logger.error(f"DinaBot: polling error: {e}")
            # Явно закрываем приложение при ошибке
            await self._app.shutdown()
            await self._app._bootstrap_initialize()
        finally:
            # Не пытаемся управлять event loop - run_polling сам всё закроет
            pass

    # ============================================================
    # Вспомогательный метод для отправки сообщений с экранированием
    # ============================================================

    async def _reply(self, update: Update, text: str, **kwargs):
        """Отправляет ответ с экранированным текстом."""
        escaped = self._escape(text)
        await update.message.reply_text(escaped, parse_mode=ParseMode.MARKDOWN_V2, **kwargs)

    # ============================================================
    # Middleware
    # ============================================================

    def _is_allowed(self, update: Update) -> bool:
        if not self.cfg.allowed_ids:
            return True
        chat_id = update.effective_chat.id
        return chat_id in self.cfg.allowed_ids

    async def _guard(self, update: Update) -> bool:
        if not self._is_allowed(update):
            await update.message.reply_text("⛔ Доступ запрещён.")
            return False
        if self._owner_chat_id is None:
            self._owner_chat_id = update.effective_chat.id
        return True

    # ============================================================
    # Команды (все используют _reply с экранированием)
    # ============================================================

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        state_emoji = {BotState.RUNNING: "🟢", BotState.PAUSED: "🟡", BotState.HALTED: "🔴"}
        text = (
            f"Дина — торговый бот\n\n"
            f"Статус: {state_emoji[self.state]} {self.state.value}\n\n"
            f"Команды:\n"
            f"/status — позиция и риски\n"
            f"/history — последние сделки\n"
            f"/pnl — статистика P&L\n"
            f"/pause — пауза\n"
            f"/resume — возобновить\n"
            f"/close — закрыть позицию\n"
            f"/risk — параметры риска\n"
            f"/setlimit 3.0 — дневной лимит потерь %\n"
            f"/attribution — P&L по источникам сигналов"
        )
        await self._reply(update, text)

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        text = await self._build_status()
        await self._reply(update, text)

    async def _cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        trades = await self._load_trades(limit=10)
        if not trades:
            await update.message.reply_text("Сделок пока нет.")
            return
        lines = ["Последние сделки:\n"]
        for t in trades:
            ts = time.strftime("%m-%d %H:%M", time.localtime(t["ts"]))
            side = "🟢 L" if t.get("direction") == "long" else "🔴 S"
            pnl = t.get("pnl_usd", 0)
            sign = "+" if pnl >= 0 else ""
            exit_r = t.get("exit_reason", "?")
            lines.append(f"{ts} {side} @{t['entry_price']:.1f}→{t['exit_price']:.1f} \\| {sign}{pnl:.2f}$ \\[{exit_r}\\]")
        await self._reply(update, "\n".join(lines))

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        stats = await self._calc_pnl_stats()
        text = self._format_pnl(stats)
        await self._reply(update, text)

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if self.state == BotState.PAUSED:
            await update.message.reply_text("Уже на паузе.")
            return
        self.state = BotState.PAUSED
        if self.strategist:
            self.strategist._paused = True
        await self._reply(update, "🟡 Торговля приостановлена\nОткрытые позиции не закрываются.\n/resume — возобновить")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if self.state == BotState.HALTED:
            await update.message.reply_text("🔴 Бот в HALT по риск-менеджеру. Исправь проблему и перезапусти бота.")
            return
        self.state = BotState.RUNNING
        if self.strategist:
            self.strategist._paused = False
        await self._reply(update, "🟢 Торговля возобновлена")

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not self.executor or not self.executor._positions:
            await update.message.reply_text("Нет открытых позиций для закрытия.")
            return
        first_symbol = next(iter(self.executor._positions.keys()))
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Закрыть", callback_data=f"close_{first_symbol}"),
             InlineKeyboardButton("❌ Отмена", callback_data="close_cancel")]
        ])
        await update.message.reply_text(
            f"⚠️ Закрыть позицию {first_symbol}?\nОрдер будет исполнен по рынку.",
            reply_markup=keyboard,
        )

    async def _cmd_risk(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not self.risk_manager:
            await update.message.reply_text("RiskManager не подключён.")
            return
        text = self.risk_manager.status_str(self.portfolio)
        await self._reply(update, text)

    async def _cmd_setlimit(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        args = ctx.args
        if not args:
            await update.message.reply_text(f"Использование: /setlimit 3.0\nТекущий лимит: {self.risk_manager.daily_loss_limit if self.risk_manager else '?'}%")
            return
        try:
            new_limit = float(args[0])
            if not 0.5 <= new_limit <= 20:
                raise ValueError("Вне диапазона 0.5–20")
        except ValueError as e:
            await update.message.reply_text(f"❌ Неверное значение: {e}")
            return
        if self.risk_manager:
            old = self.risk_manager.daily_loss_limit
            self.risk_manager.daily_loss_limit = new_limit
            logger.info(f"Daily loss limit changed: {old}% → {new_limit}%")
        await update.message.reply_text(f"✅ Дневной лимит обновлён: {new_limit}%")

    async def _cmd_attribution(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not self.attribution:
            await update.message.reply_text("Attribution не подключён.")
            return
        report = await self.attribution.get_report(days=30)
        await self._reply(update, report)

    # ============================================================
    # Inline callback
    # ============================================================

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.data.startswith("close_"):
            symbol = query.data.split("_")[1]
            if not self.executor:
                await query.edit_message_text("❌ Executor не подключён.")
                return
            await query.edit_message_text("⏳ Закрываю позицию...")
            result = await self.executor.close_position(symbol, reason="manual")
            if result.success:
                await query.edit_message_text(f"✅ Позиция {symbol} закрыта\nЦена: {result.filled_price:.2f}\nРазмер: {result.filled_size:.6f}")
            else:
                await query.edit_message_text(f"❌ Ошибка: {result.error}")
        elif query.data == "close_cancel":
            await query.edit_message_text("Отменено.")

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update):
            return
        await update.message.reply_text("Используй команды: /status /history /pnl /pause /resume")

    # ============================================================
    # Проактивные алерты с приоритетами и ночным режимом
    # ============================================================

    async def _send(self, text: str, priority: str = "normal"):
        """Отправляет сообщение с учётом ночного режима и приоритета."""
        text = self._escape(text)   # принудительное экранирование
        if not self._owner_chat_id or not self._app:
            logger.warning(f"DinaBot: нет получателя для алерта: {text[:60]}")
            return

        now = time.gmtime()
        is_night = (now.tm_hour >= self.cfg.silent_start_hour or now.tm_hour < self.cfg.silent_end_hour)

        if priority in ("critical", "high") or not is_night:
            await self._app.bot.send_message(
                chat_id=self._owner_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            self._night_buffer.append({"text": text, "priority": priority})
            if len(self._night_buffer) == 1:
                asyncio.create_task(self._send_night_summary())

    async def _send_night_summary(self):
        now = time.gmtime()
        seconds_until_morning = ( (self.cfg.silent_end_hour - now.tm_hour) % 24 ) * 3600 - now.tm_min * 60 - now.tm_sec
        await asyncio.sleep(seconds_until_morning + 10)
        if self._night_buffer:
            summary = "🌙 Ночная сводка:\n\n" + "\n".join([item["text"] for item in self._night_buffer])
            await self._app.bot.send_message(
                chat_id=self._owner_chat_id,
                text=self._escape(summary),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            self._night_buffer.clear()

    # ============================================================
    # Алерты
    # ============================================================

    async def alert_signal(self, symbol: str, direction: str, entry_price: float, sl_price: float, tp_price: float, confidence: float, reason: str = ""):
        side_emoji = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        rr = abs(tp_price - entry_price) / abs(entry_price - sl_price) if entry_price != sl_price else 0
        text = (
            f"📡 Сигнал — {side_emoji} | {symbol}\n\n"
            f"Вход:  {entry_price:.2f}\n"
            f"SL:    {sl_price:.2f} ({abs(entry_price-sl_price)/entry_price*100:.2f}%)\n"
            f"TP:    {tp_price:.2f} ({abs(tp_price-entry_price)/entry_price*100:.2f}%)\n"
            f"R/R:   1 : {rr:.1f}\n"
            f"Conf:  {confidence:.0%}"
        )
        if reason:
            text += f"\n_{reason}"
        await self._send(text, priority="normal")

    async def alert_opened(self, symbol: str, direction: str, filled_price: float, size_usd: float, sl_price: float, tp_price: float, dry_run: bool = False):
        tag = " [DRY RUN]" if dry_run else ""
        side = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        text = (
            f"✅ Позиция открыта{tag} — {side} | {symbol}\n\n"
            f"Цена входа: {filled_price:.2f}\n"
            f"Размер:     ${size_usd:,.0f}\n"
            f"SL:         {sl_price:.2f}\n"
            f"TP:         {tp_price:.2f}"
        )
        await self._send(text, priority="high")

    async def alert_closed(self, symbol: str, direction: str, entry_price: float, exit_price: float, pnl_usd: float, pnl_pct: float, reason: str, dry_run: bool = False):
        tag = " [DRY RUN]" if dry_run else ""
        sign = "+" if pnl_usd >= 0 else ""
        emoji = "🎉" if pnl_usd >= 0 else "😔"
        reason_map = {"sl": "SL", "tp": "TP ✨", "signal": "сигнал", "manual": "вручную", "timeout": "таймаут"}
        r_str = reason_map.get(reason, reason)
        text = (
            f"{emoji} Позиция закрыта{tag} | {symbol}\n\n"
            f"Выход:  {r_str}\n"
            f"Вход:   {entry_price:.2f} → {exit_price:.2f}\n"
            f"P&L:   {sign}{pnl_usd:.2f}$ ({sign}{pnl_pct:.2f}%)"
        )
        await self._send(text, priority="high")

    async def alert_drawdown(self, drawdown_pct: float, state: str):
        emoji = "🛑" if state == "EMERGENCY" else "⚠️"
        text = f"{emoji} Drawdown alert\n\nПросадка: -{drawdown_pct:.1f}%\nСостояние: {state}"
        if state == "EMERGENCY":
            text += "\nТорговля остановлена автоматически."
        else:
            text += "\nРазмер позиций снижен автоматически."
        await self._send(text, priority="critical" if state == "EMERGENCY" else "high")

    async def alert_error(self, message: str):
        await self._send(f"🆘 Ошибка\n\n{message}", priority="critical")

    async def alert_daily_summary(self):
        if not self.portfolio:
            return
        trades = await self._load_trades(since_hours=24)
        wins = [t for t in trades if t.get("pnl_usd", 0) > 0]
        losses = [t for t in trades if t.get("pnl_usd", 0) <= 0]
        total_pnl = sum(t.get("pnl_usd", 0) for t in trades)
        sign = "+" if total_pnl >= 0 else ""
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        text = (
            f"📊 Итог дня\n\n"
            f"Сделок:  {len(trades)} (W:{len(wins)} L:{len(losses)})\n"
            f"P&L:    {sign}{total_pnl:.2f}$  {emoji}\n"
            f"Баланс:  ${self.portfolio.balance:,.2f}\n"
            f"Drawdown: -{self.portfolio.drawdown_pct:.1f}%"
        )
        await self._send(text, priority="info")

    # ============================================================
    # Построители ответов
    # ============================================================

    async def _build_status(self) -> str:
        state_emoji = {BotState.RUNNING: "🟢 RUNNING", BotState.PAUSED: "🟡 PAUSED", BotState.HALTED: "🔴 HALTED"}
        lines = [f"Статус: {state_emoji[self.state]}\n"]
        if self.executor and self.symbols:
            pos = await self.executor.get_position(self.symbols[0])
            if pos.is_open:
                pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
                lines.append(
                    f"📌 Позиция: {pos.side.value.upper()}\n"
                    f"Вход: {pos.avg_price:.2f}\n"
                    f"Размер: {pos.size} монет\n"
                    f"Unr. PnL: {pnl_sign}{pos.unrealized_pnl:.2f}$\n"
                )
            else:
                lines.append("📭 Позиций нет\n")
        if self.risk_manager and self.portfolio:
            dd = self.portfolio.drawdown_pct
            bal = self.portfolio.balance
            cl = self.portfolio.consecutive_losses
            lines.append(
                f"🛡 Риск:\n"
                f"Баланс: ${bal:,.2f}\n"
                f"Drawdown: -{dd:.1f}%\n"
                f"Серия потерь: {cl}\n"
            )
        return "\n".join(lines)

    def _format_pnl(self, stats: dict) -> str:
        def fmt_row(label, pnl, trades):
            sign = "+" if pnl >= 0 else ""
            return f"{label}: {sign}{pnl:.2f}$ ({trades} сделок)"
        lines = [
            "📈 P&L статистика\n",
            fmt_row("Сегодня", stats["day_pnl"], stats["day_trades"]),
            fmt_row("Неделя", stats["week_pnl"], stats["week_trades"]),
            fmt_row("Всё время", stats["all_pnl"], stats["all_trades"]),
            "",
            f"Win Rate: {stats['win_rate']*100:.1f}%",
            f"Best trade: +{stats['best_trade']:.2f}$",
            f"Worst trade: {stats['worst_trade']:.2f}$",
        ]
        return "\n".join(lines)

    # ============================================================
    # База данных
    # ============================================================

    async def _load_trades(self, limit: int = 10, since_hours: int = 0) -> List[dict]:
        import aiosqlite
        try:
            async with aiosqlite.connect(self.cfg.db_path) as db:
                db.row_factory = aiosqlite.Row
                if since_hours:
                    since = time.time() - since_hours * 3600
                    cur = await db.execute("SELECT * FROM order_log WHERE ts >= ? ORDER BY ts DESC", (since,))
                else:
                    cur = await db.execute("SELECT * FROM order_log ORDER BY ts DESC LIMIT ?", (limit,))
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"DinaBot: error loading trades: {e}")
            return []

    async def _calc_pnl_stats(self) -> dict:
        now = time.time()
        day = now - 86400
        week = now - 86400 * 7
        all_trades = await self._load_trades(limit=1000)
        day_trades = [t for t in all_trades if t.get("ts", 0) >= day]
        week_trades = [t for t in all_trades if t.get("ts", 0) >= week]
        pnls = [t.get("pnl_usd", 0) for t in all_trades]
        return {
            "day_pnl": sum(t.get("pnl_usd", 0) for t in day_trades),
            "day_trades": len(day_trades),
            "week_pnl": sum(t.get("pnl_usd", 0) for t in week_trades),
            "week_trades": len(week_trades),
            "all_pnl": sum(pnls),
            "all_trades": len(all_trades),
            "win_rate": len([p for p in pnls if p > 0]) / len(pnls) if pnls else 0,
            "best_trade": max(pnls) if pnls else 0,
            "worst_trade": min(pnls) if pnls else 0,
        }

    # ============================================================
    # Экранирование спецсимволов Telegram
    # ============================================================

    @staticmethod
    def _escape(text: str) -> str:
        """Экранирует спецсимволы Telegram MarkdownV2."""
        specials = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for ch in specials:
            text = text.replace(ch, f'\\{ch}')
        return text

    def stop(self):
        """Вызывается из оркестратора (из любого потока)."""
        if self._stop_event and self._tg_loop:
            self._tg_loop.call_soon_threadsafe(self._stop_event.set)