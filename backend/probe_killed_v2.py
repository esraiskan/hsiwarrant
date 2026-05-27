"""验证 today_low / today_high 在 MagnetEngine 上是否生效。"""
from __future__ import annotations

import json
import sys
import urllib.request

# Compare: /api/state for nearest_*, the live snapshot for the 25450 bulls.
state = json.loads(urllib.request.urlopen('http://127.0.0.1:6000/api/state').read())
print("nearest_bull_distance_pts:", state['cbbc_nearest_bull_distance_pts'])
print("nearest_bear_distance_pts:", state['cbbc_nearest_bear_distance_pts'])
print("magnet_bias:", state['cbbc_magnet_bias'])
print("current_price:", state['current_price'])

# day_open from market regime
try:
    reg = json.loads(urllib.request.urlopen('http://127.0.0.1:6000/api/market-regime').read() or b'null')
    if reg:
        print("day_open:", reg.get('day_open'))
        print("market regime current_price:", reg.get('current_price'))
except Exception as e:
    print(f"market-regime err: {e}")
