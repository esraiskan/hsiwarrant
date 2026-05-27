"""Probe the live HSIStrategyEngine via /api/state and direct attribute peek
through a minimal WS connection (read-only).

Purpose: see why ``cbbc_magnet_bias`` is null even though the layer is
enabled and the snapshot was seeded.
"""
from __future__ import annotations

import json
import sys
import urllib.request


def main() -> int:
    # 1. /api/state
    with urllib.request.urlopen('http://127.0.0.1:6000/api/state', timeout=5) as resp:
        state = json.loads(resp.read())
    print('current_price:', state['current_price'])
    print('cbbc_magnet_layer_enabled:', state['cbbc_magnet_layer_enabled'])
    print('cbbc_magnet_degraded:', state['cbbc_magnet_degraded'])
    print('cbbc_magnet_bias:', state['cbbc_magnet_bias'])
    print('cbbc_nearest_bull_distance_pts:', state['cbbc_nearest_bull_distance_pts'])
    print('cbbc_nearest_bear_distance_pts:', state['cbbc_nearest_bear_distance_pts'])
    print('last_magnet_consult:', state['last_magnet_consult'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
