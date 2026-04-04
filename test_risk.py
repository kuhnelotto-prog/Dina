"""
test_risk.py — проверяем что RiskManager реально блокирует.
Запускать вручную: python test_risk.py
"""
import asyncio
from risk_manager import RiskManager
from position_sizer import PositionSizer, PortfolioState

async def run():
    # Создаем портфель с чистым состоянием (без просадки)
    portfolio = PortfolioState(balance=1000, peak_balance=1000, consecutive_losses=0, total_trades=0)
    
    # Тест 1: превышение лимита открытых позиций
    # Устанавливаем max_total_exposure_usd достаточно большим, чтобы формула не блокировала
    rm1 = RiskManager(max_open_positions=1, max_total_exposure_usd=1_000_000)
    print("\n=== TEST 1: лимит открытых позиций ===")
    status1 = await rm1.check(
        portfolio=portfolio,
        symbol="BTCUSDT",
        entry_price=50000,
        sl_price=49000,
        confidence=0.8,
        direction="long"
    )
    print(f"  First position: {'ALLOWED' if status1.allowed else 'BLOCKED'} - {status1.reason}")
    if status1.allowed:
        rm1.on_trade_opened("BTCUSDT", size_usd=2000, side="long")
        print("  Position opened.")
        # Проверяем вторую позицию (должна быть заблокирована из-за max_open_positions)
        status1b = await rm1.check(
            portfolio=portfolio,
            symbol="ETHUSDT",
            entry_price=3000,
            sl_price=2900,
            confidence=0.8,
            direction="long"
        )
        print(f"  Second position: {'ALLOWED - PROBLEM!' if status1b.allowed else 'BLOCKED - OK'} - {status1b.reason}")
    else:
        print("  Unexpected block.")

    # Тест 2: дублирование позиции на тот же символ (корреляция)
    rm2 = RiskManager(max_open_positions=2, max_total_exposure_usd=1_000_000)
    rm2.on_trade_opened("BTCUSDT", size_usd=2000, side="long")
    print("\n=== TEST 2: дублирование позиции на тот же символ ===")
    status2 = await rm2.check(
        portfolio=portfolio,
        symbol="BTCUSDT",
        entry_price=50000,
        sl_price=49000,
        confidence=0.8,
        direction="long"
    )
    print(f"  Duplicate long on BTCUSDT: {'ALLOWED - PROBLEM!' if status2.allowed else 'BLOCKED - OK'} - {status2.reason}")

    # Тест 3: превышение total_exposure
    rm3 = RiskManager(max_open_positions=5, max_total_exposure_usd=3000)
    print("\n=== TEST 3: превышение total_exposure ===")
    # Открываем первую позицию 2000 USD
    rm3.on_trade_opened("BTCUSDT", size_usd=2000, side="long")
    status3 = await rm3.check(
        portfolio=portfolio,
        symbol="ETHUSDT",
        entry_price=3000,
        sl_price=2900,
        confidence=0.8,
        direction="long"
    )
    print(f"  Second position with exposure limit: {'ALLOWED - PROBLEM!' if status3.allowed else 'BLOCKED - OK'} - {status3.reason}")

    # Тест 4: PositionSizer расчет размера
    ps = PositionSizer()
    print("\n=== TEST 4: размер позиции при балансе 1000 USDT ===")
    portfolio_small = PortfolioState(balance=1000, peak_balance=1000, consecutive_losses=0)
    size_result = ps.calculate(
        portfolio=portfolio_small,
        entry_price=50000,
        sl_price=49000,
        confidence=0.8
    )
    size_btc = size_result.position_usd / 50000 if size_result.position_usd else 0
    risk_usd = size_result.position_usd * (50000 - 49000) / 50000 if size_result.position_usd else 0
    print(f"  Size at risk {size_result.risk_pct:.2f}%: {size_btc:.4f} BTC")
    print(f"  Risk in $: ${risk_usd:.2f} (should be ~$10)")
    print(f"  Decision: {size_result.decision.value}")

    # Тест 5: SL слишком далеко
    print("\n=== TEST 5: SL слишком далеко ===")
    size_result_big = ps.calculate(
        portfolio=portfolio_small,
        entry_price=50000,
        sl_price=40000,
        confidence=0.8
    )
    size_btc_big = size_result_big.position_usd / 50000 if size_result_big.position_usd else 0
    print(f"  Size at 20% SL: {size_btc_big:.6f} BTC (should be very small)")
    print(f"  Decision: {size_result_big.decision.value}")

asyncio.run(run())
