# Dina Project — Full Audit Report
**Date: 2026-04-25**

---

## 🔴 CRITICAL BUGS (Must Fix)

### 1. Backtester ADX Counter Uses Uninitialized Variables (backtester.py:875)
```python
signal_stats["long_filtered_adx"] += 1 if composite_long > threshold_long else 0
signal_stats["short_filtered_adx"] += 1 if composite < -threshold_short else 0
```
**Bug:** `composite_long` and `threshold_long` are computed LATER (lines 890-941), but used here BEFORE they exist. Either:
- These reference stale values from a previous iteration (NameError on first candle)
- Or they reference the outer-scope `composite` variable incorrectly

**Impact:** ADX filter stats are completely wrong. The reported `ADX_filtered=874 > generated=766` is mathematically impossible, confirming the counter is broken.

**Fix:** Count all signals that fail ADX check unconditionally, or move the counter AFTER composite computation.

### 2. SL_ATR_MULT_SHORT=1.5 vs SL_ATR_MULT_LONG=6.6 — Asymmetric Too Extreme
**Live system (strategist_client.py:225):** `sl_pct = atr_pct * 1.5` for BOTH directions
**Backtester (backtester.py:740):** `SL_ATR_MULT_SHORT = 1.5` for SHORT

With 4H ATR ~3%:
- SHORT SL = entry ± (3% × 1.5) = 4.5% from entry
- LONG SL = entry ± (3% × 6.6) = 19.8% from entry

**Impact:** SHORT SL is way too tight for 4H candles. A single volatile candle can hit 4.5% move. This explains the 47.3% SL-hit rate for SHORT trades.

**Live vs Backtester discrepancy:** The live system uses a fixed 1.5x multiplier for BOTH directions. The backtester has 6.6/1.5 split. These are NOT synchronized.

### 3. Trailing Logic Divergence: Backtester vs Live (CRITICAL for production)
**Backtester (_apply_trailing_4step):** Uses P8+P34 asymmetric logic:
- LONG: No trailing before TP1, TP1 at +1ATR close 30%, TSL from peak at 2.0 ATR
- SHORT: TP1 at +1ATR close 30%, SL to breakeven+0.5ATR, TSL from peak at 1.5 ATR

**Live (trailing_manager.py):** Uses TRAILING_STAGES from config.py:
- Stage 1: +0.5 ATR → breakeven
- Stage 2: +1.0 ATR → close 25%, SL at +0.5 ATR
- Stage 3: +1.5 ATR → close 25%, SL at +1.0 ATR
- Stage 4: +2.0 ATR → close everything

**Impact:** Backtest results CANNOT be replicated in production. The exit logic is completely different. This is the #1 blocker for going live.

### 4. Position Monitor: Hardcoded ATR Proxy (position_monitor.py:139)
```python
atr_value = sl_distance / 1.5
```
**Bug:** Hardcodes SL_ATR_MULT = 1.5. But LONG uses 6.6, SHORT uses 1.5. This means:
- For LONG: actual ATR = sl_distance/6.6, but monitor computes sl_distance/1.5 → ATR is 4.4x too high
- Trailing stages fire at wrong levels for LONG positions

**Impact:** Live trailing for LONG positions will be catastrophically wrong.

---

## 🟡 MEDIUM BUGS (Should Fix)

### 5. config.py Has Duplicate/Conflicting SL_ATR_MULT Definitions
- `config.py:124-125` defines `SL_ATR_MULT_LONG = 6.6` and `SL_ATR_MULT_SHORT = 1.5`
- `backtester.py:56-57` defines the SAME constants independently
- `strategist_client.py:225` uses `atr_pct * 1.5` hardcoded (ignores both)

**Impact:** Three different sources of truth for SL multiplier. Any change to one must be manually synced.

### 6. Config.py Warns About Divergence But Doesn't Fix It
```python
# ⚠️ ВНИМАНИЕ: TRAILING_STAGES используется ТОЛЬКО в trailing_manager.py (живая система).
# backtester.py с P34 использует ДРУГУЮ логику выхода (TP1/TP2/TSL от пика).
# Это расхождение нужно устранить перед переходом в продакшен.
```
Good that it's documented, but this is a production blocker.

### 7. LEVERAGE Mismatch: config.py=3 vs backtester.py=10
- `config.py:87`: `leverage: int = field(default_factory=lambda: _int("LEVERAGE", 3))`
- `backtester.py:51`: `LEVERAGE = 10`
- `position_sizer.py:55`: `leverage: int = 1` (SizerConfig default)

**Impact:** Backtester trades at 10x leverage, live system at 3x, sizer defaults to 1x. Results are not comparable.

### 8. Backtester SYMBOLS List Diverges from config.py
- `config.py:82`: 12 symbols (includes APEUSDT, ARBUSDT)
- `backtester.py:62`: 12 symbols (same list)

But `order_manager.py:35-39` QTY_PRECISIONS only has 10 symbols:
```python
QTY_PRECISIONS = {
    "BTCUSDT": 3, "ETHUSDT": 2, "BNBUSDT": 2, "SOLUSDT": 2,
    "XRPUSDT": 0, "DOGEUSDT": 0, "ADAUSDT": 0, "LINKUSDT": 1,
    "AVAXUSDT": 1, "SUIUSDT": 1,
    "APEUSDT": 0, "ARBUSDT": 1,
}
```
**Missing:** Matches now, but risk_manager.py:79-88 SECTOR_GROUPS doesn't include APEUSDT or ARBUSDT:
```python
self.SECTOR_GROUPS = {
    "L1": ["BTCUSDT", "ETHUSDT"],
    "L2": ["BNBUSDT"],
    "DeFi": ["LINKUSDT"],
    "AI": [],
    "Gaming": [],
    "Infra": ["AVAXUSDT"],
    "Meme": ["DOGEUSDT"],
    "Alt_L1": ["XRPUSDT", "SOLUSDT", "ADAUSDT", "SUIUSDT"],
}
```
APEUSDT and ARBUSDT are NOT in any sector → they bypass sector correlation checks.

### 9. Learning Engine Weights Don't Match Signal Builder
`learning_engine.py:25-39` DEFAULT_WEIGHTS:
```python
DEFAULT_WEIGHTS = {
    "rsi": 1.0, "macd": 1.0, "bb": 1.0, "trend": 1.0,
    "ema_cross": 1.0, "engulfing": 1.0, "fvg": 1.0, "sweep": 1.0,
    "volume_spike": 1.0, "onchain": 1.0, "whale": 1.0, "macro": 1.0, "deepseek": 1.0,
}
```

`signal_builder.py:63-74` _get_default_weights():
```python
self._weights = {
    "ema_cross": 1.0, "volume_spike": 1.0, "engulfing": 0.8,
    "fvg": 0.6, "macd_cross": 0.5, "rsi_filter": 0.4,
    "bb_squeeze": 0.3, "whale_confirm": 0.7, "sweep": 0.7,
}
```

**Bug:** Key names don't match! Learning engine has `"rsi"`, `"macd"`, `"bb"`, `"trend"` — signal builder has `"rsi_filter"`, `"macd_cross"`, `"bb_squeeze"`. When learning engine tries to apply weights, the keys won't match and defaults (1.0) will always be used.

### 10. Signal Builder: Learning Weights Never Applied to Composite
`signal_builder.py:138-140`:
```python
if self._learning:
    weights = await self._learning.get_weights()
    signal["weights"] = weights
```
The weights are stored in the signal dict but NEVER used in `_calculate_composite()`. The method always uses `self._weights` (default weights). Learning engine has zero effect on live signals.

### 11. Backtester: CVD Computation Uses Non-Existent Column
`backtester.py:631-648` computes CVD:
```python
taker_buy_vol = df["taker_buy_vol"]
cvd = (2 * taker_buy_vol - df["volume"]).cumsum()
```
But `fetch_binance_klines()` includes `taker_buy_vol` (field 9), while `fetch_bitget_klines()` and synthetic data do NOT. When running without Binance data, this column is missing → CVD will be all zeros or cause KeyError.

### 12. Position Sizer: Leverage=1 Default But Backtester Uses 10
`position_sizer.py:197`:
```python
position_usd = risk_usd / (sl_dist_pct / 100)  # leverage уже учтён на бирже
```
The comment says "leverage is handled on exchange" but in dry-run mode (backtester), leverage is NOT on the exchange. The backtester applies `LEVERAGE = 10` separately in its position sizing.

---

## 🟢 MINOR ISSUES (Nice to Fix)

### 13. Dead Code: `adjust_confidence` Not Implemented
`learning_engine.py:364-387`:
```python
async def adjust_confidence(self, raw_conf, bot_id, symbol, side, rsi):
    return raw_conf, 1.0, ["adjust_confidence not yet implemented"]
```
Called nowhere. Should either implement or remove.

### 14. Dead Code: `tiered_confidence_full/half` in StrategistClient
`strategist_client.py:59-76`: Parameters stored but never used in logic.

### 15. Backtester: MIN_PNL_LONG_ENABLED = False, MIN_PNL_SHORT_ENABLED = False
`backtester.py:69-70`:
```python
MIN_PNL_LONG_ENABLED = False
MIN_PNL_SHORT_ENABLED = False
```
These flags exist but the timeout check code may still reference them. If disabled, the 96h POSITION_TIMEOUT_H still applies, which may close positions too early or too late.

### 16. Risk Manager: Sector Groups Missing New Symbols
APEUSDT and ARBUSDT are in SYMBOLS but not in any SECTOR_GROUPS. `_check_correlation()` will return `True` (no sector = no block), meaning these coins bypass sector limits.

### 17. Trailing Manager: `remaining_pct` Not Synced with Actual Position
`trailing_manager.py:51`: `remaining_pct` starts at 1.0 and is decremented by `partial_close_pct`, but this is a self-tracked value that may drift from reality. After a restart, it's reconstructed from `PositionInfo` but `PositionInfo` doesn't track partial closes.

### 18. Backtester: `composite_score` Stored But Never Used for Analysis
`backtester.py:122`: `self.composite_score = 0.0` is set at position open but never populated with the actual composite score from the signal. The field exists but is always 0.

### 19. FVG Detection Only Checks 3 Candles
`indicators_calc.py:122-126`:
```python
fvg_bull = bool(low.iloc[last] > high.iloc[pprev])
fvg_bear = bool(high.iloc[last] < low.iloc[pprev])
```
This compares candle [last] with candle [pprev] (2 candles back), skipping [prev]. This is correct for FVG detection, but the variable names are confusing (pprev = -3rd from end, not -2nd).

### 20. Backtester: Peak Price Tracking Initialized to Entry
`backtester.py:124`: `self.peak_price = entry_price`
For SHORT: `peak_price` starts at entry_price and decreases. The TSL from peak formula `peak_price + 1.5*ATR` works correctly because peak is the LOWEST price reached.

---

## 📊 SUMMARY TABLE

| # | Severity | File | Issue | Impact |
|---|----------|------|-------|--------|
| 1 | 🔴 CRITICAL | backtester.py:875 | ADX counter uses uninitialized vars | Wrong stats |
| 2 | 🔴 CRITICAL | backtester.py + strategist_client | SL_ATR_MULT 6.6/1.5 vs hardcoded 1.5 | Live != backtest |
| 3 | 🔴 CRITICAL | backtester.py vs trailing_manager.py | Trailing logic completely different | Production blocker |
| 4 | 🔴 CRITICAL | position_monitor.py:139 | Hardcoded ATR=SL/1.5 | Wrong trailing for LONG |
| 5 | 🟡 MEDIUM | config.py + backtester.py | Duplicate SL_ATR_MULT defs | Sync risk |
| 6 | 🟡 MEDIUM | config.py:111 | Known divergence not fixed | Production blocker |
| 7 | 🟡 MEDIUM | config/backtester/sizer | Leverage 3 vs 10 vs 1 | Inconsistent sizing |
| 8 | 🟡 MEDIUM | risk_manager.py | APEUSDT/ARBUSDT not in sectors | Bypasses limits |
| 9 | 🟡 MEDIUM | learning_engine vs signal_builder | Weight key names don't match | Learning disabled |
| 10 | 🟡 MEDIUM | signal_builder.py:138 | Learning weights stored but not used | Learning has no effect |
| 11 | 🟡 MEDIUM | backtester.py:631 | CVD needs taker_buy_vol column | Missing data = zeros |
| 12 | 🟡 MEDIUM | position_sizer.py:197 | Leverage=1 default vs 10 in backtest | Sizing mismatch |
| 13 | 🟢 MINOR | learning_engine.py:364 | adjust_confidence stub | Dead code |
| 14 | 🟢 MINOR | strategist_client.py:59 | tiered_confidence unused | Dead code |
| 15 | 🟢 MINOR | backtester.py:69 | MIN_PNL disabled | Timeout logic unclear |
| 16 | 🟢 MINOR | risk_manager.py | New symbols no sector | Bypasses limits |
| 17 | 🟢 MINOR | trailing_manager.py | remaining_pct drift | After restart |
| 18 | 🟢 MINOR | backtester.py:122 | composite_score always 0 | No analysis |
| 19 | 🟢 MINOR | indicators_calc.py | FVG variable naming | Confusing |
| 20 | 🟢 MINOR | backtester.py:124 | Peak price init | Works but unclear |

---

## 🔧 RECOMMENDED FIX PRIORITY

1. **Fix #3 (Trailing divergence)** — Unify backtester and live trailing logic. This is a production blocker.
2. **Fix #4 (Position monitor ATR proxy)** — Make position_monitor use actual ATR from data, not SL/1.5.
3. **Fix #2 (SL multiplier sync)** — Make strategist_client use config.py SL_ATR_MULT values.
4. **Fix #1 (ADX counter)** — Move counter after composite computation or simplify.
5. **Fix #9+10 (Learning engine weights)** — Align key names and actually apply weights in signal_builder.
6. **Fix #7 (Leverage)** — Single source of truth for leverage.
7. **Fix #8 (Sector groups)** — Add APEUSDT and ARBUSDT to sectors.