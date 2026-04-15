"""
performance_attribution.py

Считает PnL по каждому типу сигнала и отвечает на вопрос:
"Какой модуль реально приносит деньги, а какой мешает?"

Хранит данные в SQLite (та же dina.db).
Отчёт можно запросить через /pnl attribution или из TelegramBot.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)


# ============================================================
# Типы сигналов
# ============================================================

class SignalSource(str, Enum):
    TECHNICAL = "technical"   # RSI / MACD / Bollinger
    ONCHAIN   = "onchain"     # OnChainModule
    WHALE     = "whale"       # WhaleTracker
    MACRO     = "macro"       # MacroModule
    DEEPSEEK  = "deepseek"    # DeepSeek filter (как решил)
    COMPOSITE = "composite"   # когда несколько совпали


# ============================================================
# Модели
# ============================================================

@dataclass
class AttributedTrade:
    trade_id: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    sources: List[SignalSource] = field(default_factory=list)
    opened_at: float = field(default_factory=time.time)
    closed_at: float = 0.0
    is_closed: bool = False
    deepseek_conf: float = 0.0


@dataclass
class SourceStats:
    source: SignalSource
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    win_rate: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0

    def update(self, pnl: float):
        self.total_trades += 1
        self.total_pnl += pnl
        self.avg_pnl = self.total_pnl / self.total_trades
        self.best_trade = max(self.best_trade, pnl)
        self.worst_trade = min(self.worst_trade, pnl)
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
        self.win_rate = self.wins / self.total_trades if self.total_trades else 0.0

    def __str__(self) -> str:
        sign = "+" if self.total_pnl >= 0 else ""
        return (
            f"{self.source.value:<12} | "
            f"{self.total_trades:>4} trades | "
            f"WR={self.win_rate*100:>5.1f}% | "
            f"avg={sign}{self.avg_pnl:>+6.2f}% | "
            f"total={sign}{self.total_pnl:>+7.2f}%"
        )


# ============================================================
# PerformanceAttribution
# ============================================================

from typing import Optional

class PerformanceAttribution:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path: str = db_path or settings.trading.db_path
        self._open_trades: Dict[str, AttributedTrade] = {}

    async def setup(self):
        """Создаёт таблицу, если её нет."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS attributed_trades (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL DEFAULT 0,
                    pnl_pct REAL DEFAULT 0,
                    pnl_usd REAL DEFAULT 0,
                    sources TEXT DEFAULT '',
                    opened_at REAL NOT NULL,
                    closed_at REAL DEFAULT 0,
                    is_closed INTEGER DEFAULT 0,
                    deepseek_conf REAL DEFAULT 0,
                    setup_type TEXT DEFAULT ''
                )
            """)
            # Миграция: добавить setup_type если таблица уже существует
            try:
                await db.execute("ALTER TABLE attributed_trades ADD COLUMN setup_type TEXT DEFAULT ''")
            except Exception:
                pass  # колонка уже существует
            await db.commit()
        logger.info("PerformanceAttribution: таблица готова")

    async def record_open(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        sources: List[SignalSource],
        deepseek_conf: float = 0.0,
        setup_type: str = "",
    ):
        trade = AttributedTrade(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            sources=sources,
            deepseek_conf=deepseek_conf,
        )
        self._open_trades[trade_id] = trade

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO attributed_trades
                (trade_id, symbol, direction, entry_price, sources, opened_at, deepseek_conf, setup_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, symbol, direction, entry_price,
                ",".join(s.value for s in sources),
                trade.opened_at, deepseek_conf, setup_type,
            ))
            await db.commit()

        logger.info(f"Attribution OPEN: {trade_id} {symbol} {direction} setup={setup_type} sources=[{','.join(s.value for s in sources)}]")

    async def record_close(
        self,
        trade_id: str,
        exit_price: float,
        pnl_pct: float,
        pnl_usd: float = 0.0,
    ):
        trade = self._open_trades.pop(trade_id, None)
        closed_at = time.time()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE attributed_trades
                SET exit_price=?, pnl_pct=?, pnl_usd=?, closed_at=?, is_closed=1
                WHERE trade_id=?
            """, (exit_price, pnl_pct, pnl_usd, closed_at, trade_id))
            await db.commit()

        logger.info(f"Attribution CLOSE: {trade_id} pnl={pnl_pct:+.2f}%")

    async def get_stats(
        self,
        days: int = 30,
    ) -> Dict[SignalSource, SourceStats]:
        """Возвращает статистику по каждому источнику сигнала."""
        since = time.time() - days * 86400

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("""
                SELECT sources, pnl_pct
                FROM attributed_trades
                WHERE is_closed=1 AND closed_at >= ?
            """, (since,))
            rows = await cur.fetchall()

        stats: Dict[SignalSource, SourceStats] = {
            s: SourceStats(source=s) for s in SignalSource
        }

        for sources_str, pnl_pct in rows:
            if not sources_str:
                continue
            for src_str in sources_str.split(","):
                try:
                    src = SignalSource(src_str.strip())
                    stats[src].update(pnl_pct)
                except ValueError:
                    pass

        # Убираем пустые
        return {s: st for s, st in stats.items() if st.total_trades > 0}

    async def get_report(self, days: int = 30) -> str:
        """Готовый текстовый отчёт для Telegram."""
        stats = await self.get_stats(days=days)
        if not stats:
            return f"📊 Нет закрытых сделок за последние {days} дней."

        sorted_stats = sorted(stats.values(), key=lambda s: s.total_pnl, reverse=True)

        lines = [
            f"📊 Performance Attribution (последние {days} дней)",
            "",
            f"{'Source':<12} | {'Trades':>6} | {'WR':>7} | {'Avg PnL':>9} | {'Total':>9}",
            "─" * 55,
        ]

        for st in sorted_stats:
            sign = "+" if st.total_pnl >= 0 else ""
            emoji = "✅" if st.total_pnl > 0 else "❌" if st.total_pnl < 0 else "➖"
            lines.append(
                f"{emoji} {st.source.value:<10} | "
                f"{st.total_trades:>6} | "
                f"{st.win_rate*100:>6.1f}% | "
                f"{st.avg_pnl:>+8.2f}% | "
                f"{st.total_pnl:>+8.2f}%"
            )

        lines.append("")
        useful = [s for s in sorted_stats if s.total_pnl > 0 and s.total_trades >= 5]
        harmful = [s for s in sorted_stats if s.total_pnl < 0 and s.total_trades >= 5]

        if useful:
            names = ", ".join(s.source.value for s in useful[:2])
            lines.append(f"\n💡 Лучшие источники: {names}")
        if harmful:
            names = ", ".join(s.source.value for s in harmful[:2])
            lines.append(f"⚠️ Убыточные источники: {names} — пересмотри вес")

        return "\n".join(lines)

    # ============================================================
    # Setup Attribution — статистика по типам сетапов
    # ============================================================

    async def get_stats_by_setup(self, days: int = 30) -> Dict[str, dict]:
        """
        Возвращает expectancy, winrate, avg_win, avg_loss, Sharpe по каждому setup_type.
        """
        since = time.time() - days * 86400

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("""
                SELECT setup_type, pnl_pct
                FROM attributed_trades
                WHERE is_closed=1 AND closed_at >= ? AND setup_type != ''
            """, (since,))
            rows = await cur.fetchall()

        # Группируем по setup_type
        from collections import defaultdict
        groups: Dict[str, list] = defaultdict(list)
        for setup_type, pnl_pct in rows:
            if setup_type:
                groups[setup_type].append(pnl_pct)

        result = {}
        for setup, pnls in groups.items():
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            total = len(pnls)
            win_rate = len(wins) / total if total > 0 else 0
            avg_win = sum(wins) / len(wins) if wins else 0
            avg_loss = sum(losses) / len(losses) if losses else 0
            expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss) if total > 0 else 0

            # Sharpe (annualized, assuming ~1 trade/day)
            import numpy as np
            pnl_arr = np.array(pnls)
            sharpe = (pnl_arr.mean() / pnl_arr.std() * np.sqrt(252)) if len(pnls) > 1 and pnl_arr.std() > 0 else 0

            result[setup] = {
                "trades": total,
                "win_rate": win_rate,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "expectancy": expectancy,
                "sharpe": float(sharpe),
                "total_pnl": sum(pnls),
            }

        return result

    def should_send_diagnostics(self, total_trades_since_last: int, threshold: int = 100) -> bool:
        """Проверяет, прошло ли threshold сделок с последнего отчёта."""
        return total_trades_since_last >= threshold

    async def format_diagnostics_message(self, days: int = 30) -> str:
        """Формирует Telegram-отчёт по setup attribution."""
        stats = await self.get_stats_by_setup(days=days)
        if not stats:
            return f"📊 Setup Diagnostics: нет данных за {days} дней."

        sorted_setups = sorted(stats.items(), key=lambda x: x[1]["expectancy"], reverse=True)

        lines = [
            f"📊 Setup Diagnostics (последние {days} дней)",
            "",
            f"{'Setup':<20} | {'Trades':>6} | {'WR':>7} | {'Expect':>8} | {'Sharpe':>7} | {'Total':>8}",
            "─" * 72,
        ]

        for setup, st in sorted_setups:
            emoji = "✅" if st["expectancy"] > 0 else "❌"
            lines.append(
                f"{emoji} {setup:<18} | "
                f"{st['trades']:>6} | "
                f"{st['win_rate']*100:>6.1f}% | "
                f"{st['expectancy']:>+7.2f}% | "
                f"{st['sharpe']:>6.2f} | "
                f"{st['total_pnl']:>+7.2f}%"
            )

        # Рекомендации
        lines.append("")
        profitable = [s for s, st in sorted_setups if st["expectancy"] > 0 and st["trades"] >= 5]
        unprofitable = [s for s, st in sorted_setups if st["expectancy"] < 0 and st["trades"] >= 5]

        if profitable:
            lines.append(f"💡 Лучшие сетапы: {', '.join(profitable[:3])}")
        if unprofitable:
            lines.append(f"⚠️ Убыточные сетапы: {', '.join(unprofitable[:3])} — пересмотри фильтры")

        return "\n".join(lines)

    async def get_deepseek_accuracy(self, days: int = 30) -> str:
        """Отдельная метрика: насколько хорошо DeepSeek фильтрует."""
        since = time.time() - days * 86400

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("""
                SELECT deepseek_conf, pnl_pct
                FROM attributed_trades
                WHERE is_closed=1 AND closed_at >= ? AND deepseek_conf > 0
            """, (since,))
            rows = await cur.fetchall()

        if not rows:
            return "DeepSeek accuracy: нет данных"

        high_conf = [(c, p) for c, p in rows if c >= 0.80]
        low_conf  = [(c, p) for c, p in rows if 0.65 <= c < 0.80]

        def fmt(group):
            if not group:
                return "нет данных"
            avg = sum(p for _, p in group) / len(group)
            wr = sum(1 for _, p in group if p > 0) / len(group)
            return f"{len(group)} сделок | WR={wr*100:.1f}% | avg={avg:+.2f}%"

        return (
            f"🧠 DeepSeek Filter Accuracy\n"
            f"High conf (≥0.80): {fmt(high_conf)}\n"
            f"Low conf  (<0.80): {fmt(low_conf)}"
        )