#!/usr/bin/env python3
"""P13 V1: risk=2%, deposit=$1000"""
import backtester
backtester.START_BALANCE = 1000.0
backtester.BASE_RISK_PCT = 2.0

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Now re-import to pick up patched values
from run_baseline_p4 import *