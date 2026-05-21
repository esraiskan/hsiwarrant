"""
回测所有 4 个极度 RSI 分支:
  1. 极度超卖/超买 (当前K线, RSI<21/>75 + 1.3x量)
  2. 非常极端均量反抽/回落 (当前K线, RSI<=16/>=85 + 1.0x量 + 回撤3点)
  3. 已完成K线事后补单 (上根RSI极端, 当前价在 -8~+12 点窗口内)
  4. 非常极端影线反转 (上根 RSI<=10/>=90, 长影线+反弹8点)

UI 配置:
  RSI(6), 21/75, TP=+20pts, SL=-50pts, 20万股
"""
import sys, math, time
sys.path.insert(0, ".")
import pandas as pd
from futu import OpenQuoteContext, RET_OK, KLType, AuType
from config import FUTU_HOST, FUTU_PORT, SYMBOL, VOL_MA_PERIOD

# UI config
RSI_LENGTH = 6
RSI_OVERSOLD = 21.0
RSI_OVERBOUGHT = 75.0
TARGET_PNL = 400
EXTREME_STOP_PNL = 1000
ER_RATIO = 10000
SHARE_COUNT = 200000
TP_POINTS = (TARGET_PNL * ER_RATIO) / SHARE_COUNT       # 20
SL_POINTS = (EXTREME_STOP_PNL * ER_RATIO) / SHARE_COUNT # 50

# Extreme constants (复刻自 strategy.py)
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

# Cooldowns
EXTREME_STOP_REVERSAL_GUARD_SECONDS = 5 * 60
SAME_SIDE_TP_COOLDOWN_SECONDS = 3 * 60


def calc_rsi(s, n):
    d = s.diff(); g = d.where(d > 0, 0.0); lo = (-d).where(d < 0, 0.0)
    return 100 - 100 / (1 + g.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean() / lo.ewm(alpha=1.0/n, min_periods=n, adjust=False).mean())


def isnan(v):
    try: return math.isnan(float(v))
    except: return True


def extreme_signal_side(rsi, mom_ratio, price, k_high, k_low):
    """复刻 _extreme_signal_side"""
    if rsi < RSI_OVERSOLD and mom_ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return "bull", "极度超卖"
    if rsi > RSI_OVERBOUGHT and mom_ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return "bear", "极度超买"
    if mom_ratio <= VERY_EXTREME_AVG_VOLUME_MULTIPLIER:
        return None, None
    if rsi <= VERY_EXTREME_RSI_OVERSOLD and price >= k_low + VERY_EXTREME_PULLBACK_POINTS:
        return "bull", "非常极端RSI均量反抽"
    if rsi >= VERY_EXTREME_RSI_OVERBOUGHT and price <= k_high - VERY_EXTREME_PULLBACK_POINTS:
        return "bear", "非常极端RSI均量回落"
    return None, None


def completed_shadow_bull(prev_row, current_price, current_rsi):
    """分支 4a: 下影反抽 (上根 RSI<=10 + 下影 + 反弹)"""
    rsi = float(prev_row["RSI"])
    if isnan(rsi) or rsi > VERY_EXTREME_SHADOW_BULL_RSI:
        return None, None
    vol = float(prev_row["vol"]); vma = float(prev_row["VOL_MA"])
    if isnan(vma) or vma <= 0:
        return None, None
    mom = vol / vma
    if mom < VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO:
        return None, None
    open_p = float(prev_row["open"]); close = float(prev_row["close"]); low = float(prev_row["low"])
    lower_shadow = min(open_p, close) - low
    rebound = close - low
    if lower_shadow < VERY_EXTREME_SHADOW_MIN_LOWER_SHADOW_POINTS:
        return None, None
    if rebound < VERY_EXTREME_SHADOW_MIN_REBOUND_POINTS:
        return None, None
    move = current_price - close
    if move > VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS:
        return None, None
    if current_rsi > VERY_EXTREME_SHADOW_BULL_RSI + EXTREME_COMPLETED_K_RSI_BUFFER:
        return None, None
    return "bull", "非常极端下影反抽"


def completed_shadow_bear(prev_row, current_price, current_rsi):
    """分支 4b: 上影回落"""
    rsi = float(prev_row["RSI"])
    if isnan(rsi) or rsi < VERY_EXTREME_SHADOW_BEAR_RSI:
        return None, None
    vol = float(prev_row["vol"]); vma = float(prev_row["VOL_MA"])
    if isnan(vma) or vma <= 0:
        return None, None
    mom = vol / vma
    if mom < VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO:
        return None, None
    open_p = float(prev_row["open"]); close = float(prev_row["close"]); high = float(prev_row["high"])
    upper_shadow = high - max(open_p, close)
    pullback = high - close
    if upper_shadow < VERY_EXTREME_SHADOW_MIN_UPPER_SHADOW_POINTS:
        return None, None
    if pullback < VERY_EXTREME_SHADOW_MIN_PULLBACK_POINTS:
        return None, None
    move = current_price - close
    if move < -VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS:
        return None, None
    if current_rsi < VERY_EXTREME_SHADOW_BEAR_RSI - EXTREME_COMPLETED_K_RSI_BUFFER:
        return None, None
    return "bear", "非常极端上影回落"


def completed_extreme_signal(prev_row, current_price, current_rsi):
    """分支 3+4: 已完成K线事后补单"""
    rsi = float(prev_row["RSI"])
    vol = float(prev_row["vol"]); vma = float(prev_row["VOL_MA"])
    if isnan(rsi) or isnan(vma) or vma <= 0:
        return None, None
    close = float(prev_row["close"]); open_p = float(prev_row["open"])
    k_change = close - open_p
    k_body = abs(k_change)
    if k_change == 0:
        return None, None

    # 优先检查影线反转
    side, mode = completed_shadow_bull(prev_row, current_price, current_rsi)
    if side: return side, mode
    side, mode = completed_shadow_bear(prev_row, current_price, current_rsi)
    if side: return side, mode

    # K 线实体大小
    if not (MOMENTUM_MIN_K_BODY_POINTS <= k_body <= MOMENTUM_MAX_K_BODY_POINTS):
        return None, None

    mom = vol / vma
    high = float(prev_row["high"]); low = float(prev_row["low"])
    side, trigger = extreme_signal_side(rsi, mom, current_price, high, low)
    if side is None:
        return None, None

    move = current_price - close
    if side == "bear":
        if current_rsi < RSI_OVERBOUGHT - EXTREME_COMPLETED_K_RSI_BUFFER:
            return None, None
        if move > EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS:
            return None, None
        if move < -EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS:
            return None, None
        return "bear", "已完成K-" + trigger
    if side == "bull":
        if current_rsi > RSI_OVERSOLD + EXTREME_COMPLETED_K_RSI_BUFFER:
            return None, None
        if move < -EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS:
            return None, None
        if move > EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS:
            return None, None
        return "bull", "已完成K-" + trigger
    return None, None


def fetch_1m(ctx, date):
    all_bars = []; pk = None
    for _ in range(20):
        kw = dict(start=date, end=date, ktype=KLType.K_1M, autype=AuType.QFQ, max_count=50)
        if pk: kw["page_req_key"] = pk
        r, d, pk = ctx.request_history_kline(SYMBOL, **kw)
        if r != RET_OK or len(d) == 0: break
        all_bars.append(d)
        if not pk: break
        time.sleep(0.6)
    return pd.concat(all_bars) if all_bars else pd.DataFrame()


def backtest(d1):
    d1 = d1.copy()
    d1["RSI"] = calc_rsi(d1["close"], RSI_LENGTH)
    v = d1["turnover"] if "turnover" in d1.columns and d1["turnover"].sum() > 0 else d1["volume"]
    d1["vol"] = v
    d1["VOL_MA"] = v.rolling(VOL_MA_PERIOD).mean()

    pos = "none"; entry = 0.0; em = ""; et = ""
    trades = []
    last_tp_time = None; last_tp_pos = "none"
    last_ext_stop_time = None; last_ext_stop_pos = "none"; last_ext_stop_mode = ""
    last_completed_key = ""

    for i in range(max(VOL_MA_PERIOD, RSI_LENGTH) + 1, len(d1)):
        r = d1.iloc[i]; p = d1.iloc[i-1]; t = d1.index[i]
        rsi = r["RSI"]; price = r["close"]; vol = r["vol"]; vma = r["VOL_MA"]
        if isnan(rsi) or isnan(vma): continue
        ts = t.strftime("%H:%M")

        # Open filter
        if ts < "09:35" or ("13:00" <= ts < "13:05"):
            if pos == "none": continue
        # Force exit 15:55
        if ts >= "15:55" and pos != "none":
            diff = (price - entry) if pos == "bull" else (entry - price)
            pnl = (diff / ER_RATIO) * SHARE_COUNT
            trades.append(dict(r="W" if pnl>=0 else "L", pnl=pnl, d=diff, et=et, xt=ts, tp=pos, en=entry, ex=price, mode=em, forced=True))
            pos = "none"; continue

        mom = vol / vma if vma > 0 else 0
        high = float(r["high"]); low = float(r["low"])

        # Handle open position: TP/SL check
        if pos != "none":
            diff = (price - entry) if pos == "bull" else (entry - price)
            pnl = (diff / ER_RATIO) * SHARE_COUNT
            if diff >= TP_POINTS:
                trades.append(dict(r="W", pnl=pnl, d=diff, et=et, xt=ts, tp=pos, en=entry, ex=price, mode=em))
                last_tp_time = t; last_tp_pos = pos
                pos = "none"; continue
            elif diff <= -SL_POINTS:
                trades.append(dict(r="L", pnl=pnl, d=diff, et=et, xt=ts, tp=pos, en=entry, ex=price, mode=em))
                last_ext_stop_time = t; last_ext_stop_pos = pos; last_ext_stop_mode = em
                pos = "none"; continue

        if pos != "none": continue
        if ts >= "15:45": continue

        # Cooldown: same-side TP 3min
        def _tp_cooldown_ok(side):
            if last_tp_time and last_tp_pos == side:
                if (t - last_tp_time).total_seconds() < SAME_SIDE_TP_COOLDOWN_SECONDS:
                    return False
            return True

        # Cooldown: opposite-side extreme stop 5min (如果刚刚极度熊证止损，就不能马上开极度牛证)
        def _ext_stop_guard_ok(side, mode):
            if last_ext_stop_time is None: return True
            elapsed = (t - last_ext_stop_time).total_seconds()
            if elapsed >= EXTREME_STOP_REVERSAL_GUARD_SECONDS:
                return True
            if side == "bull" and last_ext_stop_mode == "极度超买" and last_ext_stop_pos == "bear":
                return False
            if side == "bear" and last_ext_stop_mode == "极度超卖" and last_ext_stop_pos == "bull":
                return False
            return True

        sig = None; mode = None

        # 1+2: 当前 K 线信号
        side, trig = extreme_signal_side(rsi, mom, price, high, low)
        if side:
            if _tp_cooldown_ok(side) and _ext_stop_guard_ok(side, trig):
                sig, mode = side, trig

        # 3+4: 已完成 K 线事后补单 (只在每根新 K 线检查一次)
        if sig is None:
            prev_key = p.name.strftime("%Y-%m-%d %H:%M:%S") if hasattr(p, "name") else str(i-1)
            if prev_key != last_completed_key:
                side, trig = completed_extreme_signal(p, price, rsi)
                if side:
                    if _tp_cooldown_ok(side) and _ext_stop_guard_ok(side, trig):
                        sig, mode = side, trig
                        last_completed_key = prev_key

        if sig:
            pos = sig; entry = price; em = mode; et = ts

    return trades


def summarize_by_mode(all_trades):
    from collections import Counter, defaultdict
    c = Counter()
    pnl_map = defaultdict(float)
    win_map = defaultdict(int)
    for t in all_trades:
        c[t["mode"]] += 1
        pnl_map[t["mode"]] += t["pnl"]
        if t["r"] == "W":
            win_map[t["mode"]] += 1
    print("\n%-28s %6s %6s %6s %8s" % ("Mode", "Total", "Wins", "Losses", "PnL"))
    print("-" * 60)
    for mode, total in sorted(c.items(), key=lambda x: -abs(pnl_map[x[0]])):
        wins = win_map[mode]
        losses = total - wins
        print("%-28s %6d %6d %6d %+8.0f" % (mode, total, wins, losses, pnl_map[mode]))


# --- Main ---
dates = ["2026-05-04","2026-05-05","2026-05-06","2026-05-07","2026-05-08","2026-05-11","2026-05-12"]
print("ALL 4 EXTREME BRANCHES - UI Config")
print("  Branches: 1=ExtRSI(<21/>75+1.3x) | 2=VExtRSI(<=16/>=85+1.0x+3pt) | 3=CompletedK | 4=Shadow")
print("  TP=+%dpts SL=-%dpts | 20万股 | Cooldowns on" % (TP_POINTS, SL_POINTS))
print("="*70)

all_trades = []
results = []
for dt in dates:
    ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
    d1 = fetch_1m(ctx, dt)
    ctx.close()
    if len(d1) == 0:
        print("%s no data" % dt); time.sleep(30); continue
    d1["time_key"] = pd.to_datetime(d1["time_key"])
    d1.set_index("time_key", inplace=True)
    chg = d1.iloc[-1]["close"] - d1.iloc[0]["open"]
    trades = backtest(d1)
    w = sum(1 for x in trades if x["r"]=="W")
    l = sum(1 for x in trades if x["r"]=="L")
    pnl = sum(x["pnl"] for x in trades) if trades else 0
    print("")
    print("%s | %+.0fpts | %dt %dW %dL %+.0f HKD" % (dt, chg, len(trades), w, l, pnl))
    for i, x in enumerate(trades, 1):
        mark = "W" if x["r"]=="W" else "L"
        print("  %s #%d %s->%s %s %.2f->%.2f %+.0fpts %+.0fHKD [%s]" % (
            mark, i, x["et"], x["xt"], x["tp"], x["en"], x["ex"], x["d"], x["pnl"], x["mode"]))
    all_trades.extend(trades)
    results.append(dict(date=dt, chg=chg, trades=len(trades), wins=w, losses=l, pnl=pnl))
    time.sleep(10)

print("")
print("="*70)
print("SUMMARY")
print("="*70)
print("%-12s %7s %7s %6s %6s %8s" % ("Date","Chg","#Trades","Wins","Losses","PnL"))
print("-"*70)
tn=tw=tl=0; tp=0.0
for r in results:
    print("%-12s %+7.0f %7d %6d %6d %+8.0f" % (r["date"], r["chg"], r["trades"], r["wins"], r["losses"], r["pnl"]))
    tn += r["trades"]; tw += r["wins"]; tl += r["losses"]; tp += r["pnl"]
print("-"*70)
wr = tw/tn*100 if tn > 0 else 0
print("%-12s %7s %7d %6d %6d %+8.0f (WR:%.0f%%)" % ("TOTAL","",tn,tw,tl,tp,wr))

summarize_by_mode(all_trades)
