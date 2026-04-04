"""
learning_engine.py
Адаптивный движок весов сигналов.
Обновляет веса только на основе сделок source='live'.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple

import aiosqlite

logger = logging.getLogger(__name__)

# ============================================================
# Константы
# ============================================================

DEFAULT_WEIGHTS: Dict[str, float] = {
    "rsi": 1.0,
    "macd": 1.0,
    "bb": 1.0,
    "trend": 1.0,
    "onchain": 1.0,
    "whale": 1.0,
    "macro": 1.0,
    "deepseek": 1.0,
}

MIN_TRADES_TOTAL = 50
MIN_TRADES_PER_SOURCE = 10
MAX_DRIFT = 0.30
DECAY_FACTOR = 0.95
DECAY_PERIOD_SECS = 7 * 86400  # одна неделя

# ============================================================
# Модели
# ============================================================

@dataclass
class StrategyWeights:
    weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    updated_at: float = field(default_factory=time.time)
    sample_size: int = 0

    def get(self, source: str) -> float:
        return self.weights.get(source, 1.0)

    def __str__(self) -> str:
        lines = [f"StrategyWeights (n={self.sample_size}):"]
        for src, w in sorted(self.weights.items()):
            delta = w - DEFAULT_WEIGHTS.get(src, 1.0)
            arrow = "↑" if delta > 0.02 else "↓" if delta < -0.02 else "="
            lines.append(f"  {src:<10} {w:.3f}  {arrow} ({delta:+.3f})")
        return "\n".join(lines)

@dataclass
class LearningStats:
    total_trades: int = 0
    trades_since_update: int = 0
    last_update_at: float = 0.0
    weights_locked: bool = True
    lock_reason: str = ""

# ============================================================
# LearningEngine
# ============================================================

class LearningEngine:
    def __init__(self,
                 db_path: Optional[str] = None,
                 min_trades_total: int = MIN_TRADES_TOTAL,
                 min_trades_per_source: int = MIN_TRADES_PER_SOURCE,
                 max_drift: float = MAX_DRIFT,
                 decay_factor: float = DECAY_FACTOR,
                 update_every_n_trades: int = 10):
        self.db_path: str = db_path or str(os.getenv("DB_PATH", "dina.db"))
        self.min_trades_total = min_trades_total
        self.min_trades_per_source = min_trades_per_source
        self.max_drift = max_drift
        self.decay_factor = decay_factor
        self.update_every_n_trades = update_every_n_trades

        self._weights: StrategyWeights = StrategyWeights()
        self._stats: LearningStats = LearningStats()
        self._lock = asyncio.Lock()

        logger.info(f"LearningEngine init | min_trades={min_trades_total} | max_drift=±{max_drift*100:.0f}% | decay={decay_factor}/week")

    async def setup(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS learning_trades (
                    trade_id TEXT PRIMARY KEY,
                    sources TEXT NOT NULL,
                    pnl_pct REAL NOT NULL,
                    closed_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS learning_weights (
                    source TEXT PRIMARY KEY,
                    weight REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            await db.commit()

        await self._load_weights()
        await self._refresh_stats()
        logger.info("LearningEngine ready")

    # ============================================================
    # Публичные методы
    # ============================================================

    async def record_trade(self, trade_id: str, sources: List[str], pnl_pct: float, closed_at: Optional[float] = None):
        """Записывает исход сделки. Вызывать только для source='live'."""
        if closed_at is None:
            closed_at = time.time()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO learning_trades
                (trade_id, sources, pnl_pct, closed_at)
                VALUES (?, ?, ?, ?)
            """, (trade_id, ",".join(sources), pnl_pct, closed_at))
            await db.commit()

        self._stats.total_trades += 1
        self._stats.trades_since_update += 1

        if self._stats.trades_since_update >= self.update_every_n_trades:
            await self._recalculate_weights()

    async def get_weights(self) -> StrategyWeights:
        return self._weights

    async def get_weight(self, source: str) -> float:
        return self._weights.get(source)

    async def get_stats(self) -> str:
        await self._refresh_stats()
        s = self._stats
        w = self._weights

        lock_note = f"🔒 Веса заблокированы — нужно {self.min_trades_total} сделок (сейчас: {s.total_trades})" if s.weights_locked else "✅ Веса активны"

        lines = [
            "🧠 LearningEngine",
            lock_note,
            f"Всего сделок: {s.total_trades}",
            f"С последнего обновления: {s.trades_since_update}",
            "",
            "Текущие веса:",
        ]
        for src in sorted(DEFAULT_WEIGHTS):
            current = w.get(src)
            delta = current - DEFAULT_WEIGHTS[src]
            arrow = "↑" if delta > 0.02 else "↓" if delta < -0.02 else "="
            lines.append(f"{src:<10} {current:.2f} {arrow} ({delta:+.3f})")
        return "\n".join(lines)

    # ============================================================
    # Внутренние методы
    # ============================================================

    async def _recalculate_weights(self):
        async with self._lock:
            if self._stats.total_trades < self.min_trades_total:
                self._stats.weights_locked = True
                self._stats.lock_reason = f"Нужно {self.min_trades_total} сделок, есть {self._stats.total_trades}"
                logger.info(f"LearningEngine: веса заблокированы — {self._stats.lock_reason}")
                self._stats.trades_since_update = 0
                return

            # Загружаем только сделки source='live' (это уже обеспечено при записи)
            rows = await self._load_trades()
            if not rows:
                return

            now = time.time()
            source_pnl: Dict[str, List[float]] = {s: [] for s in DEFAULT_WEIGHTS}

            for sources_str, pnl_pct, closed_at in rows:
                sources = [s.strip() for s in sources_str.split(",") if s.strip()]
                age_weeks = (now - closed_at) / DECAY_PERIOD_SECS
                decay = self.decay_factor ** age_weeks
                weighted_pnl = pnl_pct * decay

                for src in sources:
                    if src in source_pnl:
                        source_pnl[src].append(weighted_pnl)

            new_weights = dict(self._weights.weights)
            for src, pnl_list in source_pnl.items():
                if len(pnl_list) < self.min_trades_per_source:
                    logger.debug(f"{src} пропущен — только {len(pnl_list)} сделок (нужно {self.min_trades_per_source})")
                    continue

                avg_pnl = sum(pnl_list) / len(pnl_list)
                delta = avg_pnl * 0.05
                old_weight = new_weights.get(src, DEFAULT_WEIGHTS.get(src, 1.0))
                raw_weight = old_weight + delta
                new_weights[src] = self._clamp_weight(src, raw_weight)

                logger.debug(f"{src} {old_weight:.3f} → {new_weights[src]:.3f} (avg_pnl={avg_pnl:+.3f})")

            self._weights = StrategyWeights(weights=new_weights, updated_at=now, sample_size=self._stats.total_trades)
            self._stats.weights_locked = False
            self._stats.trades_since_update = 0
            self._stats.last_update_at = now

            await self._save_weights()
            logger.info(f"LearningEngine: веса обновлены (n={self._stats.total_trades})\n{self._weights}")

    def _clamp_weight(self, source: str, weight: float) -> float:
        default = DEFAULT_WEIGHTS.get(source, 1.0)
        lo = default * (1.0 - self.max_drift)
        hi = default * (1.0 + self.max_drift)
        return round(max(lo, min(weight, hi)), 4)

    async def _load_trades(self) -> List[Tuple[str, float, float]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT sources, pnl_pct, closed_at FROM learning_trades")
            rows = await cur.fetchall()
            return [(str(sources), float(pnl_pct), float(closed_at)) for sources, pnl_pct, closed_at in rows]

    async def _load_weights(self):
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT source, weight FROM learning_weights")
            rows = await cur.fetchall()
            if rows:
                loaded = {src: w for src, w in rows}
                merged = dict(DEFAULT_WEIGHTS)
                merged.update(loaded)
                self._weights = StrategyWeights(weights=merged)
                logger.info(f"Загружены сохранённые веса ({len(rows)} источников)")
            else:
                logger.info("Используем дефолтные веса")

    async def _save_weights(self):
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            for src, w in self._weights.weights.items():
                await db.execute("""
                    INSERT OR REPLACE INTO learning_weights (source, weight, updated_at)
                    VALUES (?, ?, ?)
                """, (src, w, now))
            await db.commit()

    async def _refresh_stats(self):
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM learning_trades")
            row = await cur.fetchone()
            self._stats.total_trades = row[0] if row else 0
            self._stats.weights_locked = self._stats.total_trades < self.min_trades_total
        # ============================================================
    # Метод для корректировки confidence (на основе RSI‑бакетов)
    # ============================================================

    async def adjust_confidence(self, raw_conf: float, bot_id: str, symbol: str,
                                 side: str, rsi: float) -> tuple[float, float, list[str]]:
        """
        Корректирует confidence на основе калибровок по RSI-бакетам.
        Возвращает (adjusted_conf, multiplier, reasons).
        """
        multipliers: List[float] = []
        reasons: List[str] = []

        # Определяем RSI‑бакет
        if rsi < 30:
            bucket = "oversold"
        elif rsi < 50:
            bucket = "below_mid"
        elif rsi < 70:
            bucket = "above_mid"
        else:
            bucket = "overbought"

        key = f"{bot_id}:rsi_{bucket}"
        # Здесь нужна калибровка, которую мы пока не храним. Для первой версии
        # можно вернуть raw_conf. Позже добавим хранение калибровок.
        # Пока возвращаем без изменений.
        return raw_conf, 1.0, ["adjust_confidence not yet implemented"]        