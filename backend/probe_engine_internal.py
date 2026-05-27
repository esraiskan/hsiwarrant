"""Probe the live MagnetEngine internals through an ad-hoc /api debug.

We don't have a debug endpoint, but we can hit /api/state several times
and check what changes. If bias remains null while current_price moves,
the engine isn't getting either snapshot or hsi_spot.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request


def get_state():
    with urllib.request.urlopen('http://127.0.0.1:6000/api/state', timeout=5) as resp:
        return json.loads(resp.read())


def main():
    for i in range(3):
        s = get_state()
        print(f'tick {i}: price={s["current_price"]} bias={s["cbbc_magnet_bias"]} '
              f'bull_d={s["cbbc_nearest_bull_distance_pts"]} bear_d={s["cbbc_nearest_bear_distance_pts"]}')
        time.sleep(2)
    return 0


if __name__ == '__main__':
    sys.exit(main())
