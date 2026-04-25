#!/usr/bin/env python3
"""Fix position sizing to use actual_sl_pct instead of sig["sl_pct"]."""
filepath = 'backtester.py'
with open(filepath, 'r', encoding='utf-8') as f:
    c = f.read()

old = 'notional_usd = risk_usd / sig["sl_pct"]'
new = 'notional_usd = risk_usd / actual_sl_pct  # P34 fix: use actual SL (with multiplier)'

if old in c:
    c = c.replace(old, new, 1)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(c)
    print("FIXED: notional_usd uses actual_sl_pct")
else:
    print("NOT FOUND")

import py_compile
try:
    py_compile.compile(filepath, doraise=True)
    print("SYNTAX OK")
except Exception as e:
    print(f"SYNTAX ERROR: {e}")