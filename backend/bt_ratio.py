import sys, math
sys.path.insert(0, ".")
import pandas as pd
from futu import OpenQuoteContext, RET_OK, KLType, AuType, SubType
from config import FUTU_HOST, FUTU_PORT, SYMBOL, RSI_LENGTH, VOL_MA_PERIOD
from config import ER_RATIO, SHARE_COUNT, STOP_POINTS, RSI_OVERSOLD, RSI_OVERBOUGHT

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
        k_chg = r["close"] - r["open"]; vol_surge = vol > vma * 1.5
        cum5 = r["cum5"] if not isnan(r["cum5"]) else 0
        day_high = d1.loc[:t, "high"].max(); day_low = d1.loc[:t, "low"].min()
        day_range = day_high - day_low; day_trend = price - open_price
        if pos == "none":
            sig = None
            if rsi < RSI_OVERSOLD and vol_hi: sig = ("bull", "RSI")
            elif RSI_OVERSOLD <= rsi < 25 and vwap_up and vol_hi and bull_pat and m15g: sig = ("bull", "RSI")
            elif rsi > RSI_OVERBOUGHT and vol_hi: sig = ("bear", "RSI")
            elif 75 < rsi <= RSI_OVERBOUGHT and vwap_dn and vol_hi and bear_pat and m15r: sig = ("bear", "RSI")
            if not sig:
                if vol_surge and k_chg > 10 and cs > 0: sig = ("bull", "Mom")
                elif vol_surge and k_chg < -10 and cs < 0: sig = ("bear", "Mom")
            if not sig and allow_cum and abs(cum5) >= 30 and day_range >= 150:
                if cum5 < -30 and cs < 0 and day_trend < 0: sig = ("bear", "Cum")
                elif cum5 > 30 and cs > 0 and day_trend > 0: sig = ("bull", "Cum")
            if sig: pos = sig[0]; entry = price; et = t.strftime("%H:%M")
        else:
            diff = (price - entry) if pos == "bull" else (entry - price)
            pnl = (diff / ER_RATIO) * SHARE_COUNT
            if diff >= STOP_POINTS: trades.append(dict(r="W", pnl=pnl)); pos = "none"
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
