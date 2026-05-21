"""
核心策略配置 - 恒指牛熊证交易系统 (实盘 OpenD 版)
"""

# ================= 核心策略配置区 =================
SYMBOL = "HK.800000"  # 行情标的：恒生指数
ER_RATIO = 10000  # 牛熊证换股比率
SHARE_COUNT = 100000  # 每次开仓数量 (10万份)
TARGET_PNL = 500  # 目标盈亏上限/下限 (500港元)
STOP_POINTS = (TARGET_PNL * ER_RATIO) / SHARE_COUNT  # 50点
EXTREME_STOP_PNL = 500  # 极度超买/超卖入场专用止损金额 (港元)
BULL_WARRANT_CODE = ""  # 牛证 number，例如 "61234" 或 "HK.61234"
BEAR_WARRANT_CODE = ""  # 熊证 number，例如 "61234" 或 "HK.61234"

# 策略参数
RSI_LENGTH = 14
RSI_OVERSOLD = 18  # RSI 超卖阈值 (更严格)
RSI_OVERBOUGHT = 82  # RSI 超买阈值 (更严格)
VOL_MA_PERIOD = 20  # 成交量均线周期
POLL_INTERVAL = 3  # 图表+策略研判间隔 (秒)
ENTRY_ORDER_WAIT_SECONDS = 40  # 买入挂单每次等待时间 (秒)
ENTRY_CUTOFF_TIME = "15:45"  # 此时间后不再开新买入单
EXTREME_RSI_STOP_VETO_ENABLED = True  # 极端 RSI 时取消当次普通止损
EXTREME_RSI_STOP_HARD_TICKS = 2  # 取消普通止损后，硬止损设为触发价 - N 格
EXTREME_RSI_STOP_REARM_TICKS = 1  # 价格回到触发价 + N 格后，普通止损重新武装

# 放量动能追价过滤
MOMENTUM_BEAR_MIN_RSI = 28
MOMENTUM_BULL_MAX_RSI = 72
MOMENTUM_BEAR_MAX_BREADTH_RATIO = 2.0
MOMENTUM_BULL_MIN_BREADTH_RATIO = 0.5

# 累积趋势市宽方向过滤
CUM_TREND_BULL_MIN_BREADTH_RATIO = 1.5
CUM_TREND_BEAR_MAX_BREADTH_RATIO = 1.0

# 富途 OpenD 配置
FUTU_HOST = "127.0.0.1"
FUTU_PORT = 11111

# 交易环境: TrdEnv.SIMULATE = 模拟盘, TrdEnv.REAL = 真实盘
# 默认使用模拟盘
TRADE_ENV = "SIMULATE"

# 交易市场
TRADE_MARKET = "HK"  # 港股市场
