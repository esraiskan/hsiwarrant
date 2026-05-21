"""
三四月回测 - 只做极度策略，标注具体分支
分支1: 极度RSI + 1.3x放量
分支2: 非常极端RSI(<=16/>=85) + 1.0x量 + 3点回抽
分支3: 已完成K线补单 (上一根K线事后触发)
分支4: 非常极端下影反抽/上影回落 (RSI<=10/>=90 + 长影)
"""
import sys, math, time as _time
sys.path.insert(0, ".")
import pandas as pd
from futu import OpenQuoteContext, RET_OK, KLType, AuType, SubType
from config import FUTU_HOST, FUTU_PORT, SYMBOL, VOL_MA_PERIOD

BT_RSI_LENGTH = 6
BT_RSI_OVERSOLD = 20
BT_RSI_OVERBOUGHT = 80
BT_STOP_POINTS = 20.0

EXTREME_VOL_MULT = 1.3
VE_RSI_OB = 85
VE_RSI_OS = 16
VE_AVG_VOL_MULT = 1.0
VE_PULLBACK = 3.0
VES_BULL_RSI = 10.0
VES_BEAR_RSI = 90.0
VES_MIN_REBOUND = 8.0
VES_MIN_LOWER = 6.0
VES_MIN_PULLBACK = 8.0
VES_MIN_UPPER = 6.0
VES_MIN_VOL = 1.0
VES_MAX_CHASE = 4.0
K_MIN = 5.0
K_MAX = 30.0
COMPL_MAX_ADVERSE = 8.0
COMPL_MAX_FAVORABLE = 12.0
COMPL_RSI_BUFFER = 5.0
TP_COOLDOWN = 180
OPEN_FILTER = [("09:30", "09:34"), ("13:00", "13:04")]
ENTRY_CUTOFF = "15:45"


def calc_rsi(s, n=6):
    d = s.diff(); g = d.where(d > 0, 0.0); lo = (-d).where(d < 0, 0.0)
    ag = g.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean()
    al = lo.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean()
    return 100.0 - 100.0 / (1.0 + ag / al)


def isnan(v):
    try: return math.isnan(float(v))
    except: return True


def in_open_filter(t):
    ts = t.strftime("%H:%M")
    for s, e in OPEN_FILTER:
        if s <= ts <= e: return True
    return False


def past_cutoff(t): return t.strftime("%H:%M") >= ENTRY_CUTOFF


def detect_signal(d1, i):
    """返回 (side, branch, desc) 或 None"""
    r = d1.iloc[i]
    rsi = r["RSI"]; price = r["close"]; vol = r["vol"]; vma = r["VOL_MA"]
    if isnan(rsi) or isnan(vma) or vma <= 0: return None
    k_chg = r["close"] - r["open"]
    if k_chg == 0: return None
    k_body = abs(k_chg)
    ratio = vol / vma
    body_ok = K_MIN <= k_body <= K_MAX

    # 分支1: 极度RSI + 1.3x量
    if body_ok:
        if rsi < BT_RSI_OVERSOLD and ratio > EXTREME_VOL_MULT:
            return ("bull", "B1", "极度超卖 RSI:%.1f %.2fx" % (rsi, ratio))
        if rsi > BT_RSI_OVERBOUGHT and ratio > EXTREME_VOL_MULT:
            return ("bear", "B1", "极度超买 RSI:%.1f %.2fx" % (rsi, ratio))

    # 分支2: 非常极端RSI + 1.0x量 + 3点回抽
    if body_ok and ratio > VE_AVG_VOL_MULT:
        if rsi <= VE_RSI_OS and price >= r["low"] + VE_PULLBACK:
            return ("bull", "B2", "非常极端反抽 RSI:%.1f %.2fx" % (rsi, ratio))
        if rsi >= VE_RSI_OB and price <= r["high"] - VE_PULLBACK:
            return ("bear", "B2", "非常极端回落 RSI:%.1f %.2fx" % (rsi, ratio))

    # 分支3 + 分支4: 已完成K线补单（看上一根K线）
    if i >= 2:
        prev = d1.iloc[i-1]
        prev_rsi = float(prev["RSI"]); prev_close = float(prev["close"])
        prev_open = float(prev["open"]); prev_high = float(prev["high"])
        prev_low = float(prev["low"]); prev_vol = float(prev["vol"])
        prev_vma = float(prev["VOL_MA"])
        if not isnan(prev_rsi) and not isnan(prev_vma) and prev_vma > 0:
            prev_k_chg = prev_close - prev_open
            if prev_k_chg != 0:
                prev_ratio = prev_vol / prev_vma

                # 分支4: 下影反抽
                if prev_rsi <= VES_BULL_RSI and prev_ratio >= VES_MIN_VOL:
                    ls = min(prev_open, prev_close) - prev_low
                    reb = prev_close - prev_low
                    if ls >= VES_MIN_LOWER and reb >= VES_MIN_REBOUND:
                        move = price - prev_close
                        if move <= VES_MAX_CHASE and rsi <= VES_BULL_RSI + COMPL_RSI_BUFFER:
                            return ("bull", "B4", "下影反抽 RSI:%.1f 反弹%.1f" % (prev_rsi, reb))

                # 分支4: 上影回落
                if prev_rsi >= VES_BEAR_RSI and prev_ratio >= VES_MIN_VOL:
                    us = prev_high - max(prev_open, prev_close)
                    pb = prev_high - prev_close
                    if us >= VES_MIN_UPPER and pb >= VES_MIN_PULLBACK:
                        move = price - prev_close
                        if move >= -VES_MAX_CHASE and rsi >= VES_BEAR_RSI - COMPL_RSI_BUFFER:
                            return ("bear", "B4", "上影回落 RSI:%.1f 回落%.1f" % (prev_rsi, pb))

                # 分支3: 已完成K普通极度补单
                prev_body = abs(prev_k_chg)
                if K_MIN <= prev_body <= K_MAX:
                    side = None
                    if prev_rsi < BT_RSI_OVERSOLD and prev_ratio > EXTREME_VOL_MULT:
                        side = "bull"
                    elif prev_rsi > BT_RSI_OVERBOUGHT and prev_ratio > EXTREME_VOL_MULT:
                        side = "bear"
                    elif prev_ratio > VE_AVG_VOL_MULT:
                        if prev_rsi <= VE_RSI_OS and prev_close >= prev_low + VE_PULLBACK:
                            side = "bull"
                        elif prev_rsi >= VE_RSI_OB and prev_close <= prev_high - VE_PULLBACK:
                            side = "bear"
                    if side:
                        move = price - prev_close
                        if side == "bear":
                            if rsi >= BT_RSI_OVERBOUGHT - COMPL_RSI_BUFFER and -COMPL_MAX_FAVORABLE <= move <= COMPL_MAX_ADVERSE:
                                return ("bear", "B3", "完成K补单 RSI:%.1f %.2fx" % (prev_rsi, prev_ratio))
                        else:
                            if rsi <= BT_RSI_OVERSOLD + COMPL_RSI_BUFFER and -COMPL_MAX_ADVERSE <= move <= COMPL_MAX_FAVORABLE:
                                return ("bull", "B3", "完成K补单 RSI:%.1f %.2fx" % (prev_rsi, prev_ratio))

    return None


def run_day(d1, date_str):
    d1["RSI"] = calc_rsi(d1["close"], BT_RSI_LENGTH)
    v = d1["turnover"] if d1["turnover"].sum() > 0 else d1["volume"]
    d1["vol"] = v; d1["VOL_MA"] = v.rolling(VOL_MA_PERIOD).mean()

    pos = "none"; entry = 0.0; trades = []; et = ""; eb = ""; edesc = ""
    last_tp_side = None; last_tp_time = None
    start_idx = max(VOL_MA_PERIOD, BT_RSI_LENGTH) + 1

    for i in range(start_idx, len(d1)):
        r = d1.iloc[i]; t = d1.index[i]
        rsi = r["RSI"]; price = r["close"]; vma = r["VOL_MA"]
        if isnan(rsi) or isnan(vma) or vma <= 0: continue
        if in_open_filter(t) or past_cutoff(t):
            if pos != "none" and t.strftime("%H:%M") >= "15:55":
                diff = (price - entry) if pos == "bull" else (entry - price)
                tag = "W" if diff >= 0 else "L"
                dur = (t - pd.Timestamp("%s %s" % (date_str, et))).total_seconds() / 60
                trades.append(dict(r=tag, d=diff, t=t.strftime("%H:%M"),
                                   tp=pos, en=entry, ex=price, dur=dur, et=et, br=eb, desc=edesc))
                pos = "none"
            continue

        if pos == "none":
            sig = detect_signal(d1, i)
            if sig:
                side, branch, desc = sig
                if last_tp_side == side and last_tp_time is not None:
                    if (t - last_tp_time).total_seconds() < TP_COOLDOWN:
                        continue
                pos = side; entry = price; et = t.strftime("%H:%M"); eb = branch; edesc = desc
        else:
            diff = (price - entry) if pos == "bull" else (entry - price)
            if diff >= BT_STOP_POINTS:
                dur = (t - pd.Timestamp("%s %s" % (date_str, et))).total_seconds() / 60
                trades.append(dict(r="W", d=diff, t=t.strftime("%H:%M"),
                                   tp=pos, en=entry, ex=price, dur=dur, et=et, br=eb, desc=edesc))
                last_tp_side = pos; last_tp_time = t
                pos = "none"
            elif diff <= -BT_STOP_POINTS:
                dur = (t - pd.Timestamp("%s %s" % (date_str, et))).total_seconds() / 60
                trades.append(dict(r="L", d=diff, t=t.strftime("%H:%M"),
                                   tp=pos, en=entry, ex=price, dur=dur, et=et, br=eb, desc=edesc))
                pos = "none"

    return trades


# Main
print("连接 OpenD...")
ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
frames = []
for start, end in [
    ("2026-03-01", "2026-03-07"), ("2026-03-08", "2026-03-14"),
    ("2026-03-15", "2026-03-21"), ("2026-03-22", "2026-03-31"),
    ("2026-04-01", "2026-04-07"), ("2026-04-08", "2026-04-14"),
    ("2026-04-15", "2026-04-21"), ("2026-04-22", "2026-04-30"),
]:
    ret, data, _ = ctx.request_history_kline(SYMBOL, start=start, end=end, ktype=KLType.K_1M, max_count=1000)
    if ret == RET_OK and data is not None and len(data) > 0:
        frames.append(data)
    _time.sleep(0.3)
ctx.close()

d1_all = pd.concat(frames, ignore_index=True)
d1_all["time_key"] = pd.to_datetime(d1_all["time_key"])
d1_all = d1_all.drop_duplicates(subset=["time_key"]).sort_values("time_key")
d1_all.set_index("time_key", inplace=True)

dates = sorted(set(d1_all.index.date))
mar_dates = [d for d in dates if d.month == 3]
apr_dates = [d for d in dates if d.month == 4]

print(f"三月: {len(mar_dates)}个交易日 | 四月: {len(apr_dates)}个交易日")
print(f"只做极度策略 | RSI(6) 20/80 | ±20点 = 固定±400 HKD")
print("=" * 95)

all_trades = {}
for date in mar_dates + apr_dates:
    d1_day = d1_all[d1_all.index.date == date].copy()
    if len(d1_day) < 30: continue
    trades = run_day(d1_day, str(date))
    all_trades[str(date)] = trades


def summarize(month_name, month_dates):
    print(f"\n{'='*95}")
    print(f"【{month_name}】按日明细:")
    print("-" * 95)
    total_w = 0; total_l = 0
    branch_stats = {"B1": [0, 0], "B2": [0, 0], "B3": [0, 0], "B4": [0, 0]}
    for date in month_dates:
        tds = all_trades.get(str(date), [])
        if not tds:
            print(f"  {date}: 无交易")
            continue
        dw = sum(1 for t in tds if t["r"] == "W")
        dl = sum(1 for t in tds if t["r"] == "L")
        total_w += dw; total_l += dl
        day_pnl = (dw - dl) * 400
        cum = (total_w - total_l) * 400
        print(f"  {date}: {len(tds)}笔 {dw}W/{dl}L PnL:{day_pnl:+.0f} 累计:{cum:+.0f}")
        for t in tds:
            tag = "W" if t["r"] == "W" else "L"
            print(f"    {tag} [{t['br']}] {t['et']}->{t['t']} {t['tp']:4s} {t['en']:.0f}->{t['ex']:.0f} {t['d']:+.0f}pts {t['dur']:.0f}min | {t['desc']}")
            if t["r"] == "W":
                branch_stats[t["br"]][0] += 1
            else:
                branch_stats[t["br"]][1] += 1

    total = total_w + total_l
    net = total_w - total_l
    print(f"\n  【{month_name}总计】{total}笔 {total_w}W/{total_l}L "
          f"胜率:{total_w/max(total,1)*100:.1f}% PnL:{net*400:+.0f} HKD (日均:{net*400/max(len(month_dates),1):+.0f})")
    print(f"\n  分支明细:")
    print(f"    B1 极度RSI+1.3x放量:   {sum(branch_stats['B1'])}笔 {branch_stats['B1'][0]}W/{branch_stats['B1'][1]}L PnL:{(branch_stats['B1'][0]-branch_stats['B1'][1])*400:+.0f}")
    print(f"    B2 非常极端RSI+回抽:   {sum(branch_stats['B2'])}笔 {branch_stats['B2'][0]}W/{branch_stats['B2'][1]}L PnL:{(branch_stats['B2'][0]-branch_stats['B2'][1])*400:+.0f}")
    print(f"    B3 已完成K线补单:       {sum(branch_stats['B3'])}笔 {branch_stats['B3'][0]}W/{branch_stats['B3'][1]}L PnL:{(branch_stats['B3'][0]-branch_stats['B3'][1])*400:+.0f}")
    print(f"    B4 非常极端下影/上影:   {sum(branch_stats['B4'])}笔 {branch_stats['B4'][0]}W/{branch_stats['B4'][1]}L PnL:{(branch_stats['B4'][0]-branch_stats['B4'][1])*400:+.0f}")
    return branch_stats, net


mar_branches, mar_net = summarize("三月", mar_dates)
apr_branches, apr_net = summarize("四月", apr_dates)

# 合并
print(f"\n{'='*95}")
print(f"【三四月合计 - 只做极度策略】")
print("-" * 95)
total_branches = {}
for k in ["B1", "B2", "B3", "B4"]:
    total_branches[k] = [mar_branches[k][0] + apr_branches[k][0], mar_branches[k][1] + apr_branches[k][1]]
for k, name in [("B1", "极度RSI+1.3x放量"),
                ("B2", "非常极端RSI+回抽"),
                ("B3", "已完成K线补单"),
                ("B4", "非常极端下影/上影")]:
    w, l = total_branches[k]
    total = w + l
    if total > 0:
        wr = w / total * 100
        print(f"  {k} {name}:   {total}笔 {w}W/{l}L 胜率:{wr:.1f}% PnL:{(w-l)*400:+.0f}")

total_w = sum(x[0] for x in total_branches.values())
total_l = sum(x[1] for x in total_branches.values())
print(f"\n  总计: {total_w+total_l}笔 {total_w}W/{total_l}L 胜率:{total_w/max(total_w+total_l,1)*100:.1f}% "
      f"PnL:{(total_w-total_l)*400:+.0f} HKD (日均:{(total_w-total_l)*400/30:+.0f})")
