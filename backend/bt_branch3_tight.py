"""
回测 4 分支 + 分支③止损收紧到 20 点
配置:
  分支① (极度超卖/超买)   : TP+20, SL bull=-50 bear=-30
  分支② (非常极端反抽/回落): TP+20, SL bull=-50 bear=-30
  分支③ (完成K-极度RSI)   : TP+20, SL=-20 (收紧)
  分支④ (完成K-影线反转)  : TP+20, SL=-20 (收紧, 和③同属"补救"类)
"""
import sys, math, time
sys.path.insert(0, ".")
import pandas as pd
from futu import OpenQuoteContext, RET_OK, KLType, AuType, SubType
from config import FUTU_HOST, FUTU_PORT, SYMBOL, VOL_MA_PERIOD

RSI_LENGTH = 6
RSI_OVERSOLD = 21.0
RSI_OVERBOUGHT = 75.0
ER_RATIO = 10000
SHARE_COUNT = 200000
TP_POINTS = 20
SL_BULL = 50        # 分支①② 做多止损
SL_BEAR = 30        # 分支①② 做空止损
SL_COMPLETED = 20   # 分支③④ 完成K线补单止损 (收紧)

EXTREME_VOLUME_SURGE_MULTIPLIER = 1.3
VERY_EXTREME_RSI_OVERBOUGHT = 85
VERY_EXTREME_RSI_OVERSOLD = 16
VERY_EXTREME_AVG_VOLUME_MULTIPLIER = 1.0
VERY_EXTREME_PULLBACK_POINTS = 3.0
VERY_EXTREME_SHADOW_BULL_RSI = 10.0
VERY_EXTREME_SHADOW_BEAR_RSI = 90.0
VERY_EXTREME_SHADOW_MIN_REBOUND_POINTS = 8.0
VERY_EXTREME_SHADOW_MIN_LOWER_SHADOW_POINTS = 6.0
VERY_EXTREME_SHADOW_MIN_PULLBACK_POINTS = 8.0
VERY_EXTREME_SHADOW_MIN_UPPER_SHADOW_POINTS = 6.0
VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO = 1.0
VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS = 4.0
MOMENTUM_MIN_K_BODY_POINTS = 5.0
MOMENTUM_MAX_K_BODY_POINTS = 30.0
EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS = 8.0
EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS = 12.0
EXTREME_COMPLETED_K_RSI_BUFFER = 5.0
SAME_SIDE_TP_COOLDOWN = 180
EXT_STOP_GUARD = 300


def calc_rsi(s, n):
    d = s.diff(); g = d.where(d > 0, 0.0); lo = (-d).where(d < 0, 0.0)
    return 100 - 100 / (1 + g.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean() / lo.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean())


def isnan(v):
    try: return math.isnan(float(v))
    except: return True


def extreme_signal_side(rsi, mom, price, k_high, k_low):
    if rsi < RSI_OVERSOLD and mom > EXTREME_VOLUME_SURGE_MULTIPLIER: return "bull", "极度超卖"
    if rsi > RSI_OVERBOUGHT and mom > EXTREME_VOLUME_SURGE_MULTIPLIER: return "bear", "极度超买"
    if mom <= VERY_EXTREME_AVG_VOLUME_MULTIPLIER: return None, None
    if rsi <= VERY_EXTREME_RSI_OVERSOLD and price >= k_low + VERY_EXTREME_PULLBACK_POINTS: return "bull", "非常极端反抽"
    if rsi >= VERY_EXTREME_RSI_OVERBOUGHT and price <= k_high - VERY_EXTREME_PULLBACK_POINTS: return "bear", "非常极端回落"
    return None, None


def completed_shadow_bull(p, price, rsi):
    r2 = float(p["RSI"])
    if isnan(r2) or r2 > VERY_EXTREME_SHADOW_BULL_RSI: return None, None
    vol = float(p["vol"]); vma = float(p["VOL_MA"])
    if isnan(vma) or vma <= 0 or vol/vma < VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO: return None, None
    o = float(p["open"]); c = float(p["close"]); lo = float(p["low"])
    ls = min(o, c) - lo; reb = c - lo
    if ls < VERY_EXTREME_SHADOW_MIN_LOWER_SHADOW_POINTS or reb < VERY_EXTREME_SHADOW_MIN_REBOUND_POINTS: return None, None
    mv = price - c
    if mv > VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS: return None, None
    if rsi > VERY_EXTREME_SHADOW_BULL_RSI + EXTREME_COMPLETED_K_RSI_BUFFER: return None, None
    return "bull", "完成K-影线反抽"


def completed_shadow_bear(p, price, rsi):
    r2 = float(p["RSI"])
    if isnan(r2) or r2 < VERY_EXTREME_SHADOW_BEAR_RSI: return None, None
    vol = float(p["vol"]); vma = float(p["VOL_MA"])
    if isnan(vma) or vma <= 0 or vol/vma < VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO: return None, None
    o = float(p["open"]); c = float(p["close"]); hi = float(p["high"])
    us = hi - max(o, c); pb = hi - c
    if us < VERY_EXTREME_SHADOW_MIN_UPPER_SHADOW_POINTS or pb < VERY_EXTREME_SHADOW_MIN_PULLBACK_POINTS: return None, None
    mv = price - c
    if mv < -VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS: return None, None
    if rsi < VERY_EXTREME_SHADOW_BEAR_RSI - EXTREME_COMPLETED_K_RSI_BUFFER: return None, None
    return "bear", "完成K-影线回落"


def completed_extreme(p, price, rsi):
    s, m = completed_shadow_bull(p, price, rsi)
    if s: return s, m
    s, m = completed_shadow_bear(p, price, rsi)
    if s: return s, m
    r2 = float(p["RSI"]); vol = float(p["vol"]); vma = float(p["VOL_MA"])
    if isnan(r2) or isnan(vma) or vma <= 0: return None, None
    c = float(p["close"]); o = float(p["open"]); kc = c - o; kb = abs(kc)
    if kc == 0 or not (MOMENTUM_MIN_K_BODY_POINTS <= kb <= MOMENTUM_MAX_K_BODY_POINTS): return None, None
    mom = vol/vma; hi = float(p["high"]); lo = float(p["low"])
    side, trig = extreme_signal_side(r2, mom, price, hi, lo)
    if side is None: return None, None
    mv = price - c
    if side == "bear":
        if rsi < RSI_OVERBOUGHT - EXTREME_COMPLETED_K_RSI_BUFFER: return None, None
        if mv > EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS or mv < -EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS: return None, None
        return "bear", "完成K-" + trig
    if side == "bull":
        if rsi > RSI_OVERSOLD + EXTREME_COMPLETED_K_RSI_BUFFER: return None, None
        if mv < -EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS or mv > EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS: return None, None
        return "bull", "完成K-" + trig
    return None, None


def fetch_1m(ctx, date):
    bars = []; pk = None
    for _ in range(20):
        kw = dict(start=date, end=date, ktype=KLType.K_1M, autype=AuType.QFQ, max_count=50)
        if pk: kw["page_req_key"] = pk
        r, d, pk = ctx.request_history_kline(SYMBOL, **kw)
        if r != RET_OK or len(d) == 0: break
        bars.append(d)
        if not pk: break
        time.sleep(0.6)
    return pd.concat(bars) if bars else pd.DataFrame()


def bt(d1):
    d1 = d1.copy()
    d1["RSI"] = calc_rsi(d1["close"], RSI_LENGTH)
    v = d1["turnover"] if "turnover" in d1.columns and d1["turnover"].sum() > 0 else d1["volume"]
    d1["vol"] = v; d1["VOL_MA"] = v.rolling(VOL_MA_PERIOD).mean()
    pos = "none"; entry = 0.0; em = ""; et = ""; trades = []
    ltp = None; ltpp = "none"; lest = None; lesp = "none"; lesm = ""; lck = ""

    for i in range(max(VOL_MA_PERIOD, RSI_LENGTH) + 1, len(d1)):
        r = d1.iloc[i]; p = d1.iloc[i-1]; t = d1.index[i]
        rsi = r["RSI"]; price = r["close"]; vol = r["vol"]; vma = r["VOL_MA"]
        if isnan(rsi) or isnan(vma): continue
        ts = t.strftime("%H:%M")
        if ts < "09:35" or ("13:00" <= ts < "13:05"):
            if pos == "none": continue
        if ts >= "15:55" and pos != "none":
            diff = (price - entry) if pos == "bull" else (entry - price); pnl = (diff / ER_RATIO) * SHARE_COUNT
            trades.append(dict(r="W" if pnl >= 0 else "L", pnl=pnl, d=diff, et=et, xt=ts, tp=pos, mode=em))
            pos = "none"; continue

        mom = vol/vma if vma > 0 else 0; hi = float(r["high"]); lo = float(r["low"])

        if pos != "none":
            diff = (price - entry) if pos == "bull" else (entry - price); pnl = (diff / ER_RATIO) * SHARE_COUNT

            # Decide SL based on entry branch
            is_completed = "完成K" in em
            if is_completed:
                sl = SL_COMPLETED
            else:
                sl = SL_BULL if pos == "bull" else SL_BEAR

            if diff >= TP_POINTS:
                trades.append(dict(r="W", pnl=pnl, d=diff, et=et, xt=ts, tp=pos, mode=em))
                ltp = t; ltpp = pos; pos = "none"; continue
            elif diff <= -sl:
                trades.append(dict(r="L", pnl=pnl, d=diff, et=et, xt=ts, tp=pos, mode=em))
                lest = t; lesp = pos; lesm = em; pos = "none"; continue

        if pos != "none": continue
        if ts >= "15:45": continue

        def tp_ok(s):
            if ltp and ltpp == s and (t-ltp).total_seconds() < SAME_SIDE_TP_COOLDOWN: return False
            return True

        def guard_ok(s):
            if lest is None: return True
            if (t-lest).total_seconds() >= EXT_STOP_GUARD: return True
            if s == "bull" and "超买" in lesm and lesp == "bear": return False
            if s == "bear" and "超卖" in lesm and lesp == "bull": return False
            return True

        sig = None; mode = None
        side, trig = extreme_signal_side(rsi, mom, price, hi, lo)
        if side and tp_ok(side) and guard_ok(side):
            sig, mode = side, trig

        if sig is None:
            pk2 = str(i - 1)
            if pk2 != lck:
                side, trig = completed_extreme(p, price, rsi)
                if side and tp_ok(side) and guard_ok(side):
                    sig, mode = side, trig; lck = pk2

        if sig: pos = sig; entry = price; em = mode; et = ts

    return trades


dates = ["2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07", "2026-05-08",
         "2026-05-11", "2026-05-12"]

print("4 分支 | TP=+%d | SL: bull=-%d bear=-%d | 完成K线 SL=-%d (收紧)" % (
    TP_POINTS, SL_BULL, SL_BEAR, SL_COMPLETED))
print("=" * 70)

all_t = []; results = []
for dt in dates:
    ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
    d1 = fetch_1m(ctx, dt)
    ctx.close()
    if len(d1) == 0:
        print("%s no data" % dt); time.sleep(30); continue
    d1["time_key"] = pd.to_datetime(d1["time_key"])
    d1.set_index("time_key", inplace=True)
    chg = d1.iloc[-1]["close"] - d1.iloc[0]["open"]
    trades = bt(d1)
    w = sum(1 for x in trades if x["r"] == "W")
    l = sum(1 for x in trades if x["r"] == "L")
    pnl = sum(x["pnl"] for x in trades) if trades else 0
    print("\n%s %+4.0f | %dt %dW %dL %+6.0f" % (dt, chg, len(trades), w, l, pnl))
    for i, x in enumerate(trades, 1):
        print("  %s #%d %s->%s %s %+.0fpts %+.0fHKD [%s]" % (
            x["r"], i, x["et"], x["xt"], x["tp"], x["d"], x["pnl"], x["mode"]))
    all_t.extend(trades)
    results.append(dict(date=dt, chg=chg, trades=len(trades), wins=w, losses=l, pnl=pnl))
    time.sleep(10)

# Today
print("\n--- Fetching today ---")
ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
ctx.subscribe([SYMBOL], [SubType.K_1M])
r1, d1 = ctx.get_cur_kline(SYMBOL, 500, ktype=KLType.K_1M, autype=AuType.QFQ)
ctx.close()
if r1 == RET_OK and len(d1) > 0:
    d1["time_key"] = pd.to_datetime(d1["time_key"])
    d1.set_index("time_key", inplace=True)
    td = d1.index[-1].date()
    d1 = d1[d1.index.date == td]
    chg = d1.iloc[-1]["close"] - d1.iloc[0]["open"]
    trades = bt(d1)
    w = sum(1 for x in trades if x["r"] == "W")
    l = sum(1 for x in trades if x["r"] == "L")
    pnl = sum(x["pnl"] for x in trades) if trades else 0
    print("\n%s (today) %+4.0f | %dt %dW %dL %+6.0f" % (td, chg, len(trades), w, l, pnl))
    for i, x in enumerate(trades, 1):
        print("  %s #%d %s->%s %s %+.0fpts %+.0fHKD [%s]" % (
            x["r"], i, x["et"], x["xt"], x["tp"], x["d"], x["pnl"], x["mode"]))
    all_t.extend(trades)
    results.append(dict(date=str(td), chg=chg, trades=len(trades), wins=w, losses=l, pnl=pnl))

print("\n" + "=" * 70)
tn = sum(r["trades"] for r in results); tw = sum(r["wins"] for r in results)
tl = sum(r["losses"] for r in results); tp = sum(r["pnl"] for r in results)
print("TOTAL: %dt %dW %dL WR:%.0f%% PnL:%+.0f HKD" % (tn, tw, tl, tw/tn*100 if tn else 0, tp))

from collections import Counter, defaultdict
c = Counter(); pm = defaultdict(float); wm = defaultdict(int)
for t in all_t:
    c[t["mode"]] += 1; pm[t["mode"]] += t["pnl"]
    wm[t["mode"]] += (1 if t["r"] == "W" else 0)
print("\n%-22s %5s %5s %5s %9s" % ("Mode", "Tot", "W", "L", "PnL"))
for m, n in sorted(c.items(), key=lambda x: -abs(pm[x[0]])):
    print("%-22s %5d %5d %5d %+9.0f" % (m, n, wm[m], n-wm[m], pm[m]))
