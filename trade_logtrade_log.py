"""
trade_log/trade_log.py
Журнал всех сделок с поддержкой source (backtest/dryrun/live) и commission.
"""

import aiosqlite
import json
import logging
import time
from datetime import datetime, timedelta
from typing import List, Optional, Dict

from .models import TradeRecord, PnLSummary

log = logging.getLogger("TradeLog")

class TradeLog:
    def __init__(self, db_path: str = "dina.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                size_usd REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                pnl_usd REAL,
                pnl_pct REAL,
                entry_time REAL NOT NULL,
                exit_time REAL,
                duration_min REAL,
                exit_reason TEXT,
                tags TEXT,
                bot_id TEXT DEFAULT 'dina_long',
                source TEXT DEFAULT 'live',
                commission REAL DEFAULT 0.0,
                commission_asset TEXT DEFAULT 'USDT'
            )
        """)
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)")
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id)")
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_source ON trades(source)")
        await self._conn.commit()
        log.info(f"TradeLog initialized: {self.db_path}")

    async def record_entry(self, trade: TradeRecord):
        await self._conn.execute("""
            INSERT INTO trades 
            (trade_id, symbol, side, size_usd, entry_price, entry_time, bot_id, tags, source, commission, commission_asset)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.trade_id,
            trade.symbol,
            trade.side,
            trade.size_usd,
            trade.entry_price,
            trade.entry_time,
            trade.bot_id,
            json.dumps(trade.tags),
            trade.source,
            trade.commission,
            trade.commission_asset,
        ))
        await self._conn.commit()
        log.debug(f"Trade entry recorded: {trade.trade_id}")

    async def record_exit(self, trade_id: str, exit_price: float, exit_reason: str = "manual",
                          commission: float = 0.0, commission_asset: str = "USDT"):
        cursor = await self._conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
        row = await cursor.fetchone()
        if not row:
            log.error(f"Trade {trade_id} not found for exit")
            return

        col_names = [d[0] for d in cursor.description]
        entry = dict(zip(col_names, row))

        entry_price = entry["entry_price"]
        size_usd = entry["size_usd"]
        side = entry["side"]

        if side == "long":
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        pnl_usd = size_usd * pnl_pct / 100
        exit_time = time.time()
        duration_min = (exit_time - entry["entry_time"]) / 60

        await self._conn.execute("""
            UPDATE trades SET
                exit_price = ?,
                pnl_usd = ?,
                pnl_pct = ?,
                exit_time = ?,
                duration_min = ?,
                exit_reason = ?,
                commission = ?,
                commission_asset = ?
            WHERE trade_id = ?
        """, (
            exit_price,
            pnl_usd,
            pnl_pct,
            exit_time,
            duration_min,
            exit_reason,
            commission,
            commission_asset,
            trade_id
        ))
        await self._conn.commit()
        log.info(f"Trade closed: {trade_id} {side} {entry['symbol']} PnL: {pnl_usd:+.2f}$")

    async def get_recent_trades(self, limit: int = 10, bot_id: Optional[str] = None, source: Optional[str] = None) -> List[TradeRecord]:
        query = "SELECT * FROM trades WHERE exit_time IS NOT NULL"
        params = []
        if bot_id:
            query += " AND bot_id = ?"
            params.append(bot_id)
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY exit_time DESC LIMIT ?"
        params.append(limit)

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            col_names = [d[0] for d in cursor.description]
            result.append(TradeRecord.from_dict(dict(zip(col_names, row))))
        return result

    async def get_pnl_for_period(self, period: str = "today", bot_id: Optional[str] = None, source: Optional[str] = None) -> PnLSummary:
        now = datetime.utcnow()
        if period == "today":
            start = datetime(now.year, now.month, now.day).timestamp()
        elif period == "week":
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif period == "month":
            start = datetime(now.year, now.month, 1).timestamp()
        else:
            start = 0

        query = """
            SELECT 
                COUNT(*) as trades,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl_usd), 0) as total_pnl,
                MAX(pnl_usd) as best_trade,
                MIN(pnl_usd) as worst_trade,
                AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END) as avg_win,
                AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd END) as avg_loss
            FROM trades 
            WHERE exit_time >= ? AND exit_time IS NOT NULL
        """
        params = [start]
        if bot_id:
            query += " AND bot_id = ?"
            params.append(bot_id)
        if source:
            query += " AND source = ?"
            params.append(source)

        cursor = await self._conn.execute(query, params)
        row = await cursor.fetchone()

        if not row or row[0] == 0:
            return PnLSummary(period=period, trades=0, wins=0, losses=0, total_pnl=0.0, win_rate=0.0)

        trades, wins, losses, total_pnl, best, worst, avg_win, avg_loss = row
        return PnLSummary(
            period=period,
            trades=trades,
            wins=wins,
            losses=losses,
            total_pnl=round(total_pnl, 2),
            win_rate=round(wins / trades * 100, 1) if trades > 0 else 0,
            best_trade=round(best, 2) if best else None,
            worst_trade=round(worst, 2) if worst else None,
            avg_win=round(avg_win, 2) if avg_win else None,
            avg_loss=round(avg_loss, 2) if avg_loss else None
        )

    async def close(self):
        if self._conn:
            await self._conn.close()