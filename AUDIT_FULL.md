# Полный аудит проекта Dina — Логические ошибки

**Дата:** 2026-04-08  
**Версия:** commit bf355d1

---

## 🔴 КРИТИЧЕСКИЕ (влияют на реальные деньги)

### 1. DataFeed подаёт данные только в signal_builder_long

**Файл:** `orchestrator.py:192`  
```python
self.data_feed = DataFeed(symbols, timeframes, signal_builder_long)
```

DataFeed обновляет кэш свечей **только** в `signal_builder_long`. `signal_builder_short` **никогда не получает данных**. Дина-Шорт работает с пустым кэшем и всегда возвращает `"error": "insufficient_data"`.

**Результат:** Дина-Шорт полностью мертва. Все шорт-сигналы = 0.

---

### 2. StrategistClient игнорирует direction — входит в лонг И шорт

**Файл:** `strategist_client.py:91-96`  
```python
if composite > 0:
    side = "long"
else:
    side = "short"
```

`StrategistClient` с `direction="LONG"` может открыть **шорт**, если composite < 0. И наоборот, `direction="SHORT"` может открыть лонг. Параметр `direction` используется только для логирования и attribution, но **не фильтрует направление входа**.

**Результат:** Дина-Лонг может открыть шорт-позицию. Два стратегиста могут открыть противоположные позиции одновременно.

---

### 3. Tiered confidence никогда не срабатывает

**Файл:** `strategist_client.py:105-111`  
```python
if adjusted_confidence >= self.tiered_confidence_full:    # 0.75
    size_pct = 5.0
elif adjusted_confidence >= self.tiered_confidence_half:  # 0.55
    size_pct = 2.5
else:
    return  # skip
```

Composite score после нормализации находится в диапазоне 0.0–0.5 (максимум ~0.465 в бэктесте). Пороги 0.55/0.75 **никогда не достигаются**. Все сигналы отбрасываются.

**Но:** `size_pct` вычисляется, но **нигде не используется**! Далее размер берётся из `risk_status.size_result.position_usd` (строка 145). Переменная `size_pct` — мёртвый код.

**Результат:** Если бы пороги были достижимы, `size_pct` всё равно ни на что не влияет.

---

### 4. Трейлинг-стоп в orchestrator не получает current_price

**Файл:** `orchestrator.py:436`  
```python
current_price = pos["current_price"]  # должен быть в pos — см. ниже
```

Метод `_update_trailing_stop` ожидает `pos["current_price"]`, но `get_open_positions()` возвращает данные с биржи, где такого поля **нет**. Будет `KeyError`, и трейлинг-стоп **никогда не работает**.

**Результат:** Все 4 шага трейлинга (breakeven, partial close, final close) не выполняются. Позиции закрываются только по SL/TP.

---

### 5. on_trade_closed в orchestrator передаёт pnl=0

**Файл:** `orchestrator.py:584`  
```python
self.risk_manager.on_trade_closed(0.0, symbol)
```

Когда монитор обнаруживает закрытие позиции, он передаёт `pnl=0` в risk_manager. Реальный PnL неизвестен.

**Результат:** `_daily_pnl` в RiskManager всегда 0. Дневной лимит потерь **никогда не срабатывает**. `PortfolioState.consecutive_losses` не обновляется из монитора.

---

### 6. Leverage не применяется в position_sizer

**Файл:** `position_sizer.py:191-194`  
```python
leverage = cfg.leverage
if leverage < 1:
    leverage = 1
# leverage вычислен, но НЕ ИСПОЛЬЗУЕТСЯ далее
risk_usd = portfolio.balance * risk_pct / 100
position_usd = risk_usd / (sl_dist_pct / 100)
```

Переменная `leverage` вычисляется, но **не участвует в расчёте `position_usd`**. При leverage=3 позиция должна быть в 3 раза больше, но этого не происходит.

**Результат:** Leverage из .env игнорируется. Размер позиции всегда как при leverage=1.

---

## 🟡 СЕРЬЁЗНЫЕ (влияют на качество торговли)

### 7. MACD cross отключён хардкодом

**Файл:** `signal_builder.py:212`  
```python
macd_cross = False  # Временно отключаем MACD cross
```

MACD cross всегда False. Вес `macd_cross: 0.5` в composite score никогда не используется, но **входит в max_possible** (знаменатель нормализации), занижая все остальные сигналы.

**Результат:** Все composite scores занижены на ~10% из-за мёртвого веса MACD в знаменателе.

---

### 8. RSI вычисляется в indicators, но не передаётся в signals dict

**Файл:** `signal_builder.py:225`  
```python
signals = {
    "rsi": rsi,  # ← значение RSI передаётся
    ...
}
```

RSI передаётся как число, но в `_calculate_composite` проверяется `signals.get("rsi", 50)`. Это работает корректно **сейчас**, но в `compute()` (строка 115) RSI передаётся в `signal` dict, а не в `signals` dict из `_calculate_signal_from_indicators`. Два разных словаря с разными путями.

---

### 9. Корреляционный фильтр слишком агрессивен

**Файл:** `risk_manager.py:191-217`

Группа "L1" включает BTC, ETH, SOL, AVAX, MATIC — 5 из 10 символов. Если открыта позиция по BTC, **все остальные L1 заблокированы**. При max_positions=2, фактически можно открыть максимум 2 позиции из разных групп.

Но XRPUSDT, ADAUSDT, DOGEUSDT, BNBUSDT, DOTUSDT **не входят ни в одну группу** (DOGE в группе "Meme" как DOGEUSDT, но в groups написано "DOGEUSDT" — нужно проверить точное совпадение). BNBUSDT, XRPUSDT, ADAUSDT, DOTUSDT — не в группах, всегда разрешены.

---

### 10. SafetyGuard использует raw API поля, а executor возвращает другой формат

**Файл:** `safety_guard.py:81-82`  
```python
side = pos.get("holdSide", "long")
entry = float(pos.get("openPriceAvg") or pos.get("averageOpenPrice") or 0)
```

SafetyGuard ожидает raw Bitget API поля (`holdSide`, `openPriceAvg`, `cTime`), но `executor.get_open_positions()` может возвращать нормализованный формат (`side`, `entry_price`). Если форматы не совпадают, все проверки SafetyGuard **молча пропускаются** (entry=0 → return).

---

### 11. Exposure check в RiskManager использует неправильную формулу

**Файл:** `risk_manager.py:106`  
```python
if total_exposure + (entry_price * self.sizer.cfg.base_risk_pct / 100 * portfolio.balance) > self.max_total_exposure_usd:
```

Формула `entry_price * base_risk_pct / 100 * balance` не имеет смысла. Для BTC при entry=70000, risk=1%, balance=10000: `70000 * 0.01 * 10000 = 7,000,000`. Это всегда превышает лимит $5000.

**Результат:** При реальных ценах BTC этот фильтр **всегда блокирует** вход. Система работает только потому, что `total_exposure` = 0 (позиции не трекаются корректно).

---

## 🟠 СРЕДНИЕ (потенциальные проблемы)

### 12. PortfolioState.update() не вызывается из монитора

Баланс обновляется с биржи каждые 5 минут (`orchestrator.py:521`), но `portfolio.update(pnl_usd)` вызывается только из `strategist_client.on_trade_closed()`, который **никогда не вызывается** (нет механизма callback из executor при закрытии по SL/TP).

**Результат:** `consecutive_losses`, `total_trades`, `recent_pnl` в PortfolioState всегда = 0. Все множители в PositionSizer (streak, Kelly) не работают.

---

### 13. Два SignalBuilder с shared_signal_time создают race condition

**Файл:** `orchestrator.py:155`  
```python
shared_signal_time: Dict[str, float] = {}
```

Оба SignalBuilder (long и short) делят один словарь cooldown. Если Дина-Лонг обновила cooldown для BTCUSDT, Дина-Шорт не сможет сгенерировать сигнал 5 минут. Это **намеренно**, но при asyncio без блокировок возможна гонка.

---

### 14. Backtest не тестирует шорты

Бэктестер (`backtester.py`) всегда входит только в лонг. Нет возможности протестировать шорт-стратегию. Composite score для шорта (отрицательный) игнорируется.

---

### 15. indicators_calc.py — ema_fast_prev/ema_slow_prev могут быть 0

Если в `indicators_calc.py` нет вычисления `ema_fast_prev` и `ema_slow_prev`, то `indicators.get("ema_fast_prev", 0)` всегда = 0. EMA cross detection сравнивает текущие EMA с 0, а не с предыдущими значениями.

**Результат:** EMA cross bull/bear определяется неправильно — любое пересечение EMA > 0 считается "бычьим кроссом".

---

## 📋 МЁРТВЫЙ КОД

| Файл | Строка | Описание |
|------|--------|----------|
| `strategist_client.py:106` | `size_pct = 5.0` | Вычисляется, но не используется |
| `strategist_client.py:99-102` | `adjusted_confidence` | LearningEngine adjust никогда не вызывается |
| `position_sizer.py:191-194` | `leverage` | Вычисляется, но не применяется |
| `signal_builder.py:212` | `macd_cross = False` | Хардкод, вес в знаменателе |
| `orchestrator.py:192` | DataFeed → signal_builder_long | signal_builder_short не получает данных |

---

## 🎯 ПРИОРИТЕТ ИСПРАВЛЕНИЙ

1. **#1 + #2** — DataFeed для обоих SignalBuilder + фильтр direction в StrategistClient
2. **#4** — Трейлинг-стоп: добавить current_price из markPrice
3. **#5 + #12** — Callback при закрытии позиции → обновление PnL, portfolio, risk_manager
4. **#11** — Исправить формулу exposure check
5. **#6** — Применить leverage в position_sizer
6. **#3** — Убрать мёртвый size_pct или привязать к реальному размеру
7. **#7** — Убрать MACD из max_possible или включить его