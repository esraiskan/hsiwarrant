import sys, math
sys.path.insert(0, ".")
import pandas as pd
from futu import OpenQuoteContext, RET_OK, KLType, AuType, SubType
from config import FUTU_HOST, FUTU_PORT, SYMBOL, RSI_LENGTH, VOL_MA_PERIOD
from config import ER_RATIO, SHARE_COUNT, STOP_POINTS, RSI_OVERSOLD, RSI_OVERBOUGHT
from trend_filter import (
    CUM_TREND_BOUNDARY_POINTS,
    get_cum_trend_boundary_filter_reasons,
)

EXTREME_VOLUME_SURGE_MULTIPLIER = 1.4
VERY_EXTREME_RSI_OVERBOUGHT = 85
VERY_EXTREME_RSI_OVERSOLD = 16
VERY_EXTREME_VOLUME_SURGE_MULTIPLIER = 1.25
VERY_EXTREME_AVG_VOLUME_MULTIPLIER = 1.0
VERY_EXTREME_PULLBACK_POINTS = 3.0
MOMENTUM_VOLUME_SURGE_MULTIPLIER = 1.5
MOMENTUM_MIN_K_BODY_POINTS = 5.0
MOMENTUM_MAX_K_BODY_POINTS = 30.0


def extreme_signal(rsi, ratio, price, high, low):
    if rsi < RSI_OVERSOLD and ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return "bull", "RSI"
    if rsi > RSI_OVERBOUGHT and ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return "bear", "RSI"
    if ratio <= VERY_EXTREME_AVG_VOLUME_MULTIPLIER:
        return None
    if rsi <= VERY_EXTREME_RSI_OVERSOLD and price >= low + VERY_EXTREME_PULLBACK_POINTS:
        return "bull", "VeryExtremeLowPullback"
    if rsi >= VERY_EXTREME_RSI_OVERBOUGHT and price <= high - VERY_EXTREME_PULLBACK_POINTS:
        return "bear", "VeryExtremeHighPullback"
    return None

def calc_rsi(s, n=14):
    d = s.diff(); g = d.where(d > 0, 0.0); lo = (-d).where(d < 0, 0.0)
    return 100 - 100 / (1 + g.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean() / lo.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean())
def calc_vwap(h, l, c, v):
    tp = (h + l + c) / 3.0
    if v.sum() == 0: return tp.expanding().mean()
    return (tp * v).cumsum() / v.cumsum()
def isnan(v):
    try: return math.isnan(float(v))
    except: return True

# Precomputed daily ratios (from earlier analysis)
daily_ratio = {
    "2026-04-20": 1.95, "2026-04-21": 2.69, "2026-04-22": 0.36,
    "2026-04-23": 0.57, "2026-04-24": 1.14, "2026-04-27": 0.90,
    "2026-04-28": 0.50, "2026-04-29": 7.57, "2026-04-30": 0.22,
}

def bt(d1, d15, allow_cum):
    d1["RSI"] = calc_rsi(d1["close"], RSI_LENGTH)
    v = d1["turnover"] if "turnover" in d1.columns and d1["turnover"].sum() > 0 else d1["volume"]
    d1["VWAP"] = calc_vwap(d1["high"], d1["low"], d1["close"], v)
    d1["VWAP_SLOPE"] = d1["VWAP"].diff(); d1["vol"] = v
    d1["VOL_MA"] = v.rolling(VOL_MA_PERIOD).mean()
    d1["cum5"] = d1["close"].diff().rolling(5).sum()
    open_price = d1.iloc[0]["open"]
    pos = "none"; entry = 0.0; trades = []; et = ""
    last_take_profit_side = None; last_take_profit_time = None
    for i in range(max(VOL_MA_PERIOD, RSI_LENGTH) + 1, len(d1)):
        r = d1.iloc[i]; p = d1.iloc[i-1]; t = d1.index[i]
        rsi = r["RSI"]; price = r["close"]; vol = r["vol"]; vma = r["VOL_MA"]
        if isnan(rsi) or isnan(vma): continue
        cs = r["VWAP_SLOPE"]; ps = p["VWAP_SLOPE"] if not isnan(p["VWAP_SLOPE"]) else 0
        vwap_up = (ps <= 0 and cs > 0) or (cs > 0 and cs > ps)
        vwap_dn = (ps >= 0 and cs < 0) or (cs < 0 and cs < ps)
        vol_hi = vol > vma; kb = abs(r["close"] - r["open"])
        ls = min(r["open"], r["close"]) - r["low"]; us = r["high"] - max(r["open"], r["close"])
        bull_pat = (r["close"] > r["open"]) or (ls > kb * 1.0)
        bear_pat = (r["close"] < r["open"]) or (us > kb * 1.0)
        m15c = d15[d15.index <= t]
        if len(m15c) == 0: continue
        m15 = m15c.iloc[-1]; m15g = m15["close"] > m15["open"]; m15r = m15["close"] < m15["open"]
        k_chg = r["close"] - r["open"]
        momentum_ratio = vol / vma if vma > 0 else 0.0
        momentum_body_ok = MOMENTUM_MIN_K_BODY_POINTS <= abs(k_chg) <= MOMENTUM_MAX_K_BODY_POINTS
        extreme_sig = extreme_signal(rsi, momentum_ratio, price, r["high"], r["low"]) if k_chg != 0 and momentum_body_ok else None
        vol_surge = vol > vma * MOMENTUM_VOLUME_SURGE_MULTIPLIER
        cum5 = r["cum5"] if not isnan(r["cum5"]) else 0
        day_high = d1.loc[:t, "high"].max(); day_low = d1.loc[:t, "low"].min()
        day_range = day_high - day_low; day_trend = price - open_price
        if pos == "none":
            sig = None
            if extreme_sig: sig = extreme_sig
            elif RSI_OVERSOLD <= rsi < 25 and vwap_up and vol_hi and bull_pat and m15g: sig = ("bull", "RSI")
            elif 75 < rsi <= RSI_OVERBOUGHT and vwap_dn and vol_hi and bear_pat and m15r: sig = ("bear", "RSI")
            if not sig:
                if vol_surge and k_chg > 10 and cs > 0: sig = ("bull", "Mom")
                elif vol_surge and k_chg < -10 and cs < 0: sig = ("bear", "Mom")
            if not sig and allow_cum and abs(cum5) > CUM_TREND_BOUNDARY_POINTS and day_range >= 150:
                recent = d1.iloc[max(0, i-5):i+1]
                recent_low = recent["low"].min(); recent_high = recent["high"].max()
                prev_close = p["close"]
                if cum5 < -CUM_TREND_BOUNDARY_POINTS and cs < 0 and day_trend < 0 and rsi >= RSI_OVERSOLD:
                    boundary_reasons = get_cum_trend_boundary_filter_reasons(
                        "bear", float(cum5), float(rsi), float(prev_close), float(price),
                        float(recent_low), float(recent_high), RSI_OVERSOLD, RSI_OVERBOUGHT,
                    )
                    if not boundary_reasons: sig = ("bear", "Cum")
                elif cum5 > CUM_TREND_BOUNDARY_POINTS and cs > 0 and day_trend > 0 and rsi <= RSI_OVERBOUGHT:
                    boundary_reasons = get_cum_trend_boundary_filter_reasons(
                        "bull", float(cum5), float(rsi), float(prev_close), float(price),
                        float(recent_low), float(recent_high), RSI_OVERSOLD, RSI_OVERBOUGHT,
                    )
                    if not boundary_reasons: sig = ("bull", "Cum")
            if sig and last_take_profit_side == sig[0] and last_take_profit_time is not None:
                if (t - last_take_profit_time).total_seconds() < 180:
                    sig = None
            if sig: pos = sig[0]; entry = price; et = t.strftime("%H:%M")
        else:
            diff = (price - entry) if pos == "bull" else (entry - price)
            pnl = (diff / ER_RATIO) * SHARE_COUNT
            if diff >= STOP_POINTS: trades.append(dict(r="W", pnl=pnl)); last_take_profit_side = pos; last_take_profit_time = t; pos = "none"
            elif diff <= -STOP_POINTS: trades.append(dict(r="L", pnl=pnl)); pos = "none"
    w = sum(1 for x in trades if x["r"] == "W"); lo = sum(1 for x in trades if x["r"] == "L")
    return len(trades), w, lo, sum(x["pnl"] for x in trades) if trades else 0

dates = ["2026-04-20","2026-04-21","2026-04-22","2026-04-23","2026-04-24",
         "2026-04-27","2026-04-28","2026-04-29"]

print("%-10s | %5s | %5s | %4s | %-18s | %-18s | %s" % ("Date","Ratio","Type","Cum?","ALWAYS ON","FILTERED","Diff"))
print("-" * 90)

r_on = []; r_off = []
for dt in dates:
    ratio = daily_ratio.get(dt, 1.0)
    is_chop = 0.5 <= ratio <= 2.0
    tag = "CHOP" if is_chop else "TRND"

    ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
    r1, d1, _ = ctx.request_history_kline(SYMBOL, start=dt, end=dt, ktype=KLType.K_1M, autype=AuType.QFQ, max_count=1000)
    r2, d15, _ = ctx.request_history_kline(SYMBOL, start=dt, end=dt, ktype=KLType.K_15M, autype=AuType.QFQ, max_count=100)
    ctx.close()
    if r1 != RET_OK or len(d1) == 0: continue
    d1["time_key"] = pd.to_datetime(d1["time_key"]); d1.set_index("time_key", inplace=True)
    d15["time_key"] = pd.to_datetime(d15["time_key"]); d15.set_index("time_key", inplace=True)

    n1, w1, l1, p1 = bt(d1.copy(), d15.copy(), allow_cum=True)
    n2, w2, l2, p2 = bt(d1.copy(), d15.copy(), allow_cum=not is_chop)
    r_on.append(p1); r_off.append(p2)
    print("%-10s | %5.2f | %4s | %4s | %2dt %dW %dL %+6.0f | %2dt %dW %dL %+6.0f | %+5.0f" % (
        dt, ratio, tag, "OFF" if is_chop else "ON", n1, w1, l1, p1, n2, w2, l2, p2, p2-p1))

# Today
ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
ctx.subscribe([SYMBOL], [SubType.K_1M, SubType.K_15M])
r1, d1 = ctx.get_cur_kline(SYMBOL, 500, ktype=KLType.K_1M, autype=AuType.QFQ)
r2, d15 = ctx.get_cur_kline(SYMBOL, 50, ktype=KLType.K_15M, autype=AuType.QFQ)
ctx.close()
d1["time_key"] = pd.to_datetime(d1["time_key"]); d1.set_index("time_key", inplace=True)
td = d1.index[-1].date(); d1 = d1[d1.index.date == td]
d15["time_key"] = pd.to_datetime(d15["time_key"]); d15.set_index("time_key", inplace=True)
ratio = 0.22; is_chop = 0.5 <= ratio <= 2.0
n1, w1, l1, p1 = bt(d1.copy(), d15.copy(), allow_cum=True)
n2, w2, l2, p2 = bt(d1.copy(), d15.copy(), allow_cum=not is_chop)
r_on.append(p1); r_off.append(p2)
print("%-10s | %5.2f | %4s | %4s | %2dt %dW %dL %+6.0f | %2dt %dW %dL %+6.0f | %+5.0f" % (
    "2026-04-30", ratio, "TRND", "ON", n1, w1, l1, p1, n2, w2, l2, p2, p2-p1))

print("-" * 90)
print("%-10s |       |      |      | %18s | %18s | %+5.0f" % (
    "TOTAL", "%+.0f HKD" % sum(r_on), "%+.0f HKD" % sum(r_off), sum(r_off)-sum(r_on)))
