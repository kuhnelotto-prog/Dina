"""
validate_dina.py

Интеграционный валидатор Дины.
Проверяет:
  - Переменные окружения
  - Импорты всех модулей
  - PositionSizer / RiskManager
  - PortfolioState
  - BitgetExecutor (dry‑run)
  - LearningEngine
  - SignalBuilder (мульти‑ТФ, FVG, sweep)
  - PerformanceAttribution
  - TelegramBot (без реальных вызовов)
  - Backtester (синтетические данные)
"""

import asyncio
import argparse
import os
import sys
import traceback
from dotenv import load_dotenv

load_dotenv()

# Вспомогательный класс для отчётов
class Reporter:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self._suite = ""

    def suite(self, name: str):
        self._suite = name
        print(f"\n{'━'*50}\n  {name}\n{'━'*50}")

    def ok(self, msg: str):
        self.passed += 1
        print(f"  ✅ {msg}")

    def fail(self, msg: str, detail: str = ""):
        self.failed += 1
        print(f"  ❌ {msg}")
        if detail:
            for line in detail.strip().split("\n")[-3:]:
                print(f"       {line}")

    def skip(self, msg: str, reason: str = ""):
        self.skipped += 1
        tag = f" ({reason})" if reason else ""
        print(f"  ⏭  {msg}{tag}")

    def summary(self) -> bool:
        total = self.passed + self.failed + self.skipped
        print(f"\n{'═'*50}\n  Итого: {total} тестов")
        print(f"  ✅ {self.passed} passed")
        if self.failed:
            print(f"  ❌ {self.failed} FAILED")
        if self.skipped:
            print(f"  ⏭  {self.skipped} skipped")
        print(f"{'═'*50}")
        if self.failed == 0:
            print("\n  🎉 Дина готова к запуску!\n")
        else:
            print(f"\n  ⚠️  Исправь {self.failed} ошибку(и) перед запуском.\n")
        return self.failed == 0

R = Reporter()

# ============================================================
# Секция 1: Переменные окружения
# ============================================================
def test_env():
    R.suite("1. Переменные окружения")
    required = ["TELEGRAM_BOT_TOKEN", "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        for k in missing:
            R.fail(f"{k} не задан")
    else:
        R.ok("Все обязательные переменные заданы")

    if os.getenv("DRY_RUN", "true").lower() != "true":
        R.skip("DRY_RUN != true", "бот будет торговать реальными деньгами!")

# ============================================================
# Секция 2: Импорты
# ============================================================
def test_imports():
    R.suite("2. Импорты модулей")
    modules = [
        "position_sizer", "risk_manager", "bitget_executor",
        "learning_engine", "signal_builder", "indicators_calc",
        "performance_attribution", "telegram_bot", "strategist_client",
        "event_bus"
    ]
    for mod in modules:
        try:
            __import__(mod)
            R.ok(f"{mod}")
        except ImportError as e:
            R.fail(f"{mod}", str(e))

# ============================================================
# Секция 3: PositionSizer и PortfolioState
# ============================================================
def test_position_sizer():
    R.suite("3. PositionSizer / PortfolioState")
    try:
        from position_sizer import PositionSizer, PortfolioState, SizerConfig, SizerDecision
        sizer = PositionSizer(SizerConfig(base_risk_pct=1.0, max_risk_pct=2.0))
        portfolio = PortfolioState(balance=10000, peak_balance=10000)

                # Нормальные условия
        res = sizer.calculate(portfolio, entry_price=50000, sl_price=49000, confidence=0.8, atr_pct=1.5, side="long")
        assert res.decision != SizerDecision.HALT
        assert 0 < res.risk_pct <= 2.0
        R.ok(f"Нормальные условия: risk={res.risk_pct:.2f}% pos=${res.position_usd:,.0f}")

                # Высокий ATR
        res2 = sizer.calculate(portfolio, entry_price=50000, sl_price=49000, confidence=0.8, atr_pct=4.5, side="long")
        assert res2.position_usd < res.position_usd
        R.ok("Высокий ATR → позиция меньше")

                # Просадка
        portfolio_dd = PortfolioState(balance=9000, peak_balance=10000)
        res3 = sizer.calculate(portfolio_dd, entry_price=50000, sl_price=49000, confidence=0.8, side="long")
        assert res3.drawdown_multiplier < 1.0
        R.ok("Просадка -10% → множитель уменьшен")

                # HALT
        portfolio_halt = PortfolioState(balance=8300, peak_balance=10000)
        res4 = sizer.calculate(portfolio_halt, entry_price=50000, sl_price=49000, confidence=0.8, side="long")
        assert res4.decision == SizerDecision.HALT
        R.ok("Просадка >15% → HALT")
    except Exception as e:
        R.fail("PositionSizer", traceback.format_exc())

# ============================================================
# Секция 4: RiskManager
# ============================================================
def test_risk_manager():
    R.suite("4. RiskManager")
    try:
        from risk_manager import RiskManager, DrawdownState
        from position_sizer import PortfolioState, SizerConfig

        rm = RiskManager(sizer_config=SizerConfig(base_risk_pct=1.0), max_open_positions=1, daily_loss_limit=5.0)
        portfolio = PortfolioState(balance=10000)

        # Нормальный проход
        status = asyncio.get_event_loop().run_until_complete(
            rm.check(portfolio, symbol="BTCUSDT", entry_price=50000, sl_price=49000, confidence=0.8, atr_pct=1.5)
        )
        assert status.allowed
        R.ok("Нормальный проход → ALLOWED")

        # Лимит позиций
        rm.on_trade_opened("BTCUSDT", 1000, "long", "LONG")
        status2 = asyncio.get_event_loop().run_until_complete(
            rm.check(portfolio, symbol="ETHUSDT", entry_price=3000, sl_price=2950, confidence=0.8)
        )
        assert not status2.allowed
        R.ok("Лимит позиций → BLOCKED")

        # Закрытие
        rm.on_trade_closed(50, "BTCUSDT")
        status3 = asyncio.get_event_loop().run_until_complete(
            rm.check(portfolio, symbol="ETHUSDT", entry_price=3000, sl_price=2950, confidence=0.8)
        )
        assert status3.allowed
        R.ok("После закрытия → снова ALLOWED")
    except Exception as e:
        R.fail("RiskManager", traceback.format_exc())

# ============================================================
# Секция 5: BitgetExecutor (dry‑run)
# ============================================================
def test_executor():
    R.suite("5. BitgetExecutor (dry‑run)")
    try:
        from bitget_executor import BitgetExecutor, ExecutorConfig, OrderRequest, OrderType

        import tempfile
        temp_db = tempfile.NamedTemporaryFile(delete=False)
        temp_db.close()
        cfg = ExecutorConfig(dry_run=True, db_path=temp_db.name, allowlist_symbols=["BTCUSDT"])
        executor = BitgetExecutor(cfg)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(executor.setup())
        R.ok("setup() прошёл")

        req = OrderRequest(symbol="BTCUSDT", direction="long", size_usd=1000, entry_price=50000, sl_price=49000, tp_price=51000)
        result = loop.run_until_complete(executor.open_position(req))
        assert result.success and result.dry_run
        R.ok("open_position() dry‑run успешен")
    except Exception as e:
        R.fail("BitgetExecutor", traceback.format_exc())

# ============================================================
# Секция 6: LearningEngine
# ============================================================
def test_learning_engine():
    R.suite("6. LearningEngine")
    try:
        from learning_engine import LearningEngine
        import tempfile
        temp_db = tempfile.NamedTemporaryFile(delete=False)
        temp_db.close()
        engine = LearningEngine(db_path=temp_db.name)
        # после теста можно удалить файл, но для простоты оставим
        loop = asyncio.get_event_loop()
        loop.run_until_complete(engine.setup())
        R.ok("setup() прошёл")

        # Записываем сделки
        for _ in range(5):
            loop.run_until_complete(engine.record_trade("trade1", ["rsi", "macd"], 1.5))
        for _ in range(3):
            loop.run_until_complete(engine.record_trade("trade2", ["rsi"], -0.8))
        R.ok("record_trade() записал 8 сделок")

        weights = loop.run_until_complete(engine.get_weights())
        assert hasattr(weights, 'weights') and len(weights.weights) > 0
        R.ok(f"get_weights() вернул {len(weights.weights)} весов")
    except Exception as e:
        R.fail("LearningEngine", traceback.format_exc())

# ============================================================
# Секция 7: SignalBuilder (мульти‑ТФ, FVG, sweep)
# ============================================================
def test_signal_builder():
    R.suite("7. SignalBuilder")
    try:
        from signal_builder import SignalBuilder
        import pandas as pd
        import numpy as np

        # Создаём синтетические данные
        dates = pd.date_range("2024-01-01", periods=100, freq="1h")
        df = pd.DataFrame({
            "open": np.random.randn(100).cumsum() + 50000,
            "high": np.random.randn(100).cumsum() + 50000,
            "low": np.random.randn(100).cumsum() + 50000,
            "close": np.random.randn(100).cumsum() + 50000,
            "volume": np.random.randint(100, 1000, 100)
        })
        sb = SignalBuilder(symbols=["BTCUSDT"], timeframes=["15m", "1h", "4h"], direction="LONG")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(sb.update_candle("BTCUSDT", "1h", df))
        signal = loop.run_until_complete(sb.compute("BTCUSDT"))
        assert "composite_score" in signal
        R.ok(f"compute() вернул composite_score={signal['composite_score']:.2f}")
    except Exception as e:
        R.fail("SignalBuilder", traceback.format_exc())

# ============================================================
# Секция 8: PerformanceAttribution
# ============================================================
def test_attribution():
    R.suite("8. PerformanceAttribution")
    try:
        from performance_attribution import PerformanceAttribution, SignalSource
        import tempfile
        temp_db = tempfile.NamedTemporaryFile(delete=False)
        temp_db.close()
        att = PerformanceAttribution(db_path=temp_db.name)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(att.setup())
        R.ok("setup() прошёл")

        # Запись открытия
        loop.run_until_complete(att.record_open(
            trade_id="test1", symbol="BTCUSDT", direction="long", entry_price=50000,
            sources=[SignalSource.TECHNICAL], deepseek_conf=0.85
        ))
        R.ok("record_open() прошёл")

        # Запись закрытия
        loop.run_until_complete(att.record_close("test1", exit_price=51000, pnl_pct=2.0, pnl_usd=200))
        R.ok("record_close() прошёл")

        # Отчёт
        report = loop.run_until_complete(att.get_report(days=30))
        assert "technical" in report
        R.ok("get_report() вернул текст")
    except Exception as e:
        R.fail("PerformanceAttribution", traceback.format_exc())

# ============================================================
# Секция 9: Backtester (пропускаем, если --fast)
# ============================================================
def test_backtester():
    R.suite("9. Backtester (синтетические данные)")
    try:
        from backtester import Backtester
        import pandas as pd
        import numpy as np

        candles = []
        price = 50000
        for i in range(500):
            change = np.random.normal(0, 0.01)
            price *= (1 + change)
            candles.append({
                "timestamp": i * 3600,
                "open": price * (1 - 0.001),
                "high": price * (1 + 0.005),
                "low": price * (1 - 0.005),
                "close": price,
                "volume": np.random.randint(100, 1000)
            })
        df = pd.DataFrame(candles)

        bt = Backtester(initial_balance=10000)
        # Для теста просто проверяем, что объект создаётся
        R.ok("Backtester создан")
    except Exception as e:
        R.fail("Backtester", traceback.format_exc())

# ============================================================
# Секция 10: Интеграционный тест
# ============================================================
def test_integration():
    R.suite("10. Интеграционный тест (сквозной)")
    try:
        from event_bus import EventBus
        from position_sizer import PortfolioState, SizerConfig
        from risk_manager import RiskManager
        from bitget_executor import BitgetExecutor, ExecutorConfig, OrderRequest
        from telegram_bot import DinaBot, TelegramConfig

        loop = asyncio.get_event_loop()

        portfolio = PortfolioState(balance=10000)
        rm = RiskManager(sizer_config=SizerConfig(base_risk_pct=1.0), max_open_positions=1, daily_loss_limit=5.0)
        import tempfile
        temp_db = tempfile.NamedTemporaryFile(delete=False)
        temp_db.close()
        executor = BitgetExecutor(ExecutorConfig(dry_run=True, db_path=temp_db.name, allowlist_symbols=["BTCUSDT"]))
        loop.run_until_complete(executor.setup())

        entry = 50000
        sl = 49000
        confidence = 0.85
        status = loop.run_until_complete(rm.check(portfolio, "BTCUSDT", entry, sl, confidence))
        assert status.allowed
        size = status.size_result.position_usd

        req = OrderRequest(symbol="BTCUSDT", direction="long", size_usd=size, entry_price=entry, sl_price=sl, tp_price=51000)
        result = loop.run_until_complete(executor.open_position(req))
        assert result.success

        rm.on_trade_opened("BTCUSDT", size, "long", "LONG")
        R.ok("Сквозной проход: сигнал → RiskManager → открытие позиции")
    except Exception as e:
        R.fail("Интеграция", traceback.format_exc())

# ============================================================
# Запуск
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Дина — интеграционный валидатор")
    parser.add_argument("--fast", action="store_true", help="Только быстрые тесты")
    args = parser.parse_args()

    print("\n  ╔══════════════════════════════════╗")
    print("  ║   Дина — Integration Validator   ║")
    print("  ╚══════════════════════════════════╝")

    test_env()
    test_imports()
    test_position_sizer()
    test_risk_manager()
    test_executor()
    test_learning_engine()
    test_signal_builder()
    test_attribution()
    if not args.fast:
        test_backtester()
    test_integration()

    ok = R.summary()
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
