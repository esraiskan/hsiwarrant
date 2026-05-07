import sys, math
sys.path.insert(0, ".")
import pandas as pd
from futu import OpenQuoteContext, RET_OK, KLType, AuType, SubType
from config import FUTU_HOST, FUTU_PORT, SYMBOL, RSI_LENGTH, VOL_MA_PERIOD
from config import ER_RATIO, SHARE_COUNT, STOP_POINTS, TARGET_PNL
from config import RSI_OVERSOLD, RSI_OVERBOUGHT

def calc_rsi(s, n=14):
    d = s.diff()
    g = d.where(d > 0, 0.0)
    lo = (-d).where(d < 0, 0.0)
    ag = g.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean()
    al = lo.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean()
    return 100.0 - 100.0 / (1.0 + ag / al)

def calc_vwap(h, l, c, v):
    tp = (h + l + c) / 3.0
    if v.sum() == 0:
        return tp.expanding().mean()
    return (tp * v).cumsum() / v.cumsum()

def isnan(v):
    try:
        return math.isnan(float(v))
    except Exception:
        return True

print("Connecting...")
ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
ctx.subscribe([SYMBOL], [SubType.K_1M, SubType.K_15M])
r1, d1 = ctx.get_cur_kline(SYMBOL, 500, ktype=KLType.K_1M, autype=AuType.QFQ)
r2, d15 = ctx.get_cur_kline(SYMBOL, 50, ktype=KLType.K_15M, autype=AuType.QFQ)
ctx.close()

if r1 != RET_OK or r2 != RET_OK:
    print("Data fetch failed"); sys.exit(1)

d1["time_key"] = pd.to_datetime(d1["time_key"])
d1.set_index("time_key", inplace=True)
today = d1.index[-1].date()
d1 = d1[d1.index.date == today]

d1["RSI"] = calc_rsi(d1["close"], RSI_LENGTH)
v = d1["turnover"] if d1["turnover"].sum() > 0 else d1["volume"]
d1["VWAP"] = calc_vwap(d1["high"], d1["low"], d1["close"], v)
d1["VWAP_SLOPE"] = d1["VWAP"].diff()
d1["vol"] = v
d1["VOL_MA"] = v.rolling(VOL_MA_PERIOD).mean()
d1["cum5"] = d1["close"].diff().rolling(5).sum()

d15["time_key"] = pd.to_datetime(d15["time_key"])
d15.set_index("time_key", inplace=True)

print("Date: %s | Bars: %d | %s~%s" % (today, len(d1), d1.index[0].strftime("%H:%M"), d1.index[-1].strftime("%H:%M")))
print("TP/SL: +/-%s pts = +/-%s HKD" % (STOP_POINTS, TARGET_PNL))
print("=" * 70)

pos = "none"; entry = 0.0; trades = []; et = ""
start_idx = max(VOL_MA_PERIOD, RSI_LENGTH) + 1

for i in range(start_idx, len(d1)):
    r = d1.iloc[i]; p = d1.iloc[i-1]; t = d1.index[i]
    rsi = r["RSI"]; price = r["close"]; vol = r["vol"]; vma = r["VOL_MA"]
    if isnan(rsi) or isnan(vma): continue

    cs = r["VWAP_SLOPE"]; ps = p["VWAP_SLOPE"] if not isnan(p["VWAP_SLOPE"]) else 0
    vwap_up = (ps <= 0 and cs > 0) or (cs > 0 and cs > ps)
    vwap_dn = (ps >= 0 and cs < 0) or (cs < 0 and cs < ps)
    vol_hi = vol > vma
    kb = abs(r["close"] - r["open"])
    ls = min(r["open"], r["close"]) - r["low"]
    us = r["high"] - max(r["open"], r["close"])
    bull_pat = (r["close"] > r["open"]) or (ls > kb * 1.0)
    bear_pat = (r["close"] < r["open"]) or (us > kb * 1.0)
    m15c = d15[d15.index <= t]
    if len(m15c) == 0: continue
    m15 = m15c.iloc[-1]
    m15g = m15["close"] > m15["open"]; m15r = m15["close"] < m15["open"]
    k_chg = r["close"] - r["open"]; vol_surge = vol > vma * 1.5
    cum5 = r["cum5"] if not isnan(r["cum5"]) else 0

    if pos == "none":
        sig = None
        if rsi < RSI_OVERSOLD and vol_hi:
            sig = ("bull", "ExtremeLow RSI:%.1f" % rsi)
        elif RSI_OVERSOLD <= rsi < 25 and vwap_up and vol_hi and bull_pat and m15g:
            sig = ("bull", "NormalLow RSI:%.1f" % rsi)
        elif rsi > RSI_OVERBOUGHT and vol_hi:
            sig = ("bear", "ExtremeHigh RSI:%.1f" % rsi)
        elif 75 < rsi <= RSI_OVERBOUGHT and vwap_dn and vol_hi and bear_pat and m15r:
            sig = ("bear", "NormalHigh RSI:%.1f" % rsi)
        if not sig:
            if vol_surge and k_chg > 10 and cs > 0:
                sig = ("bull", "Momentum +%.1f %.1fx" % (k_chg, vol/vma))
            elif vol_surge and k_chg < -10 and cs < 0:
                sig = ("bear", "Momentum %.1f %.1fx" % (k_chg, vol/vma))
        if not sig:
            if cum5 < -30 and cs < 0:
                sig = ("bear", "CumTrend %.1f" % cum5)
            elif cum5 > 30 and cs > 0:
                sig = ("bull", "CumTrend +%.1f" % cum5)
        if sig:
            pos, desc = sig; entry = price; et = t.strftime("%H:%M")
            d_str = "BULL" if pos == "bull" else "BEAR"
            print("[%s] >> %s (%s) @ %.2f" % (et, d_str, desc, price))
    else:
        diff = (price - entry) if pos == "bull" else (entry - price)
        pnl = (diff / ER_RATIO) * SHARE_COUNT
        if diff >= STOP_POINTS:
            dur = (t - pd.Timestamp("%s %s" % (today, et))).total_seconds() / 60
            trades.append(dict(r="W", pnl=pnl, d=diff, t=t.strftime("%H:%M"), tp=pos, en=entry, ex=price, dur=dur, et=et))
            print("[%s] WIN  %.2f->%.2f +%.2fpts +%.2fHKD %dmin" % (t.strftime("%H:%M"), entry, price, diff, pnl, dur))
            pos = "none"
        elif diff <= -STOP_POINTS:
            dur = (t - pd.Timestamp("%s %s" % (today, et))).total_seconds() / 60
            trades.append(dict(r="L", pnl=pnl, d=diff, t=t.strftime("%H:%M"), tp=pos, en=entry, ex=price, dur=dur, et=et))
            print("[%s] LOSS %.2f->%.2f %.2fpts %.2fHKD %dmin" % (t.strftime("%H:%M"), entry, price, diff, pnl, dur))
            pos = "none"

if pos != "none":
    lp = d1.iloc[-1]["close"]
    diff = (lp - entry) if pos == "bull" else (entry - lp)
    pnl = (diff / ER_RATIO) * SHARE_COUNT
    print("OPEN: %s @ %.2f now %.2f float %+.2fpts %+.2fHKD" % (pos, entry, lp, diff, pnl))

print("\n" + "=" * 70)
w = [x for x in trades if x["r"] == "W"]
lo = [x for x in trades if x["r"] == "L"]
n = len(trades)
print("Total: %d Win: %d Loss: %d" % (n, len(w), len(lo)))
if n > 0:
    print("WinRate: %.1f%% PnL: %+.2f HKD" % (len(w)/n*100, sum(x["pnl"] for x in trades)))
    for i, x in enumerate(trades, 1):
        tag = "WIN " if x["r"] == "W" else "LOSS"
        print("  #%d %s %s->%s %s %.2f->%.2f %+.2fpts %+.2fHKD %dmin" % (
            i, tag, x["et"], x["t"], x["tp"], x["en"], x["ex"], x["d"], x["pnl"], x["dur"]))
