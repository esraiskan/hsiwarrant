"""
模拟行情数据生成器
在没有富途 API 的环境下，生成逼真的恒指 K 线数据
技术指标 (RSI, VWAP) 使用纯 pandas/numpy 手动实现，无需 pandas_ta
"""
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from config import SIM_BASE_PRICE, SIM_VOLATILITY, RSI_LENGTH, VOL_MA_PERIOD


def calc_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """手动计算 RSI (Wilder 平滑法，与 pandas_ta.rsi 一致)"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    # 使用 Wilder 平滑 (即 EWM with alpha=1/length)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def calc_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """手动计算 VWAP (成交量加权平均价)"""
    typical_price = (high + low + close) / 3.0
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum()
    vwap = cum_tp_vol / cum_vol
    return vwap


class MarketSimulator:
    """模拟恒指行情数据"""

    def __init__(self):
        self.base_price = SIM_BASE_PRICE
        self.current_price = SIM_BASE_PRICE
        self.tick_count = 0
        # 预生成初始历史数据
        self._history_15m: pd.DataFrame = self._generate_initial_history(100, 15)
        self._history_30m: pd.DataFrame = self._generate_initial_history(100, 30)

    def _generate_initial_history(self, count: int, interval_minutes: int) -> pd.DataFrame:
        """生成初始历史 K 线数据"""
        now = datetime.now()
        start_time = now - timedelta(minutes=interval_minutes * count)

        records = []
        price = self.base_price

        for i in range(count):
            t = start_time + timedelta(minutes=interval_minutes * i)

            # 使用几何布朗运动模拟价格
            drift = np.random.normal(0, SIM_VOLATILITY)
            price = price * (1 + drift)

            # 生成 OHLCV
            intra_vol = SIM_VOLATILITY * 0.5
            high = price * (1 + abs(np.random.normal(0, intra_vol)))
            low = price * (1 - abs(np.random.normal(0, intra_vol)))
            open_price = price * (1 + np.random.normal(0, intra_vol * 0.3))

            # 确保 OHLC 逻辑正确
            high = max(high, open_price, price)
            low = min(low, open_price, price)

            volume = max(1000, int(np.random.normal(50000, 15000)))

            records.append({
                "time_key": t,
                "open": round(open_price, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(price, 2),
                "volume": volume,
            })

        df = pd.DataFrame(records)
        df["time_key"] = pd.to_datetime(df["time_key"])
        df.set_index("time_key", inplace=True)
        self.current_price = price
        return df

    def _generate_new_candle(self, interval_minutes: int, history: pd.DataFrame) -> dict:
        """生成新的一根 K 线"""
        self.tick_count += 1
        last_time = history.index[-1]
        new_time = last_time + timedelta(minutes=interval_minutes)

        # 引入趋势和均值回归
        mean_reversion = (self.base_price - self.current_price) / self.base_price * 0.01
        # 偶尔制造极端行情来触发 RSI 超买超卖
        extreme = 0.0
        if np.random.random() < 0.08:  # 8% 概率出现极端波动
            extreme = np.random.choice([-1, 1]) * SIM_VOLATILITY * 3

        drift = np.random.normal(0, SIM_VOLATILITY) + mean_reversion + extreme
        self.current_price = self.current_price * (1 + drift)

        intra_vol = SIM_VOLATILITY * 0.5
        open_price = self.current_price * (1 + np.random.normal(0, intra_vol * 0.3))
        high = self.current_price * (1 + abs(np.random.normal(0, intra_vol)))
        low = self.current_price * (1 - abs(np.random.normal(0, intra_vol)))

        high = max(high, open_price, self.current_price)
        low = min(low, open_price, self.current_price)

        # 极端行情时放量
        base_vol = 50000
        if abs(extreme) > 0:
            base_vol = 90000
        volume = max(1000, int(np.random.normal(base_vol, 15000)))

        return {
            "time_key": new_time,
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(self.current_price, 2),
            "volume": volume,
        }

    def get_kline_with_indicators(self, ktype: str = "15m") -> pd.DataFrame | None:
        """
        获取 K 线数据并计算技术指标 (VWAP, RSI, 成交量均线)
        完全对应原始策略中的 get_kline_with_indicators 方法
        """
        if ktype == "15m":
            history = self._history_15m
            interval = 15
        else:
            history = self._history_30m
            interval = 30

        # 生成新 K 线并追加
        new_candle = self._generate_new_candle(interval, history)
        new_row = pd.DataFrame([new_candle])
        new_row["time_key"] = pd.to_datetime(new_row["time_key"])
        new_row.set_index("time_key", inplace=True)

        if ktype == "15m":
            self._history_15m = pd.concat([history, new_row]).tail(100)
            df = self._history_15m.copy()
        else:
            self._history_30m = pd.concat([history, new_row]).tail(100)
            df = self._history_30m.copy()

        # 计算技术指标 (与原始策略完全一致)
        # 1. RSI (14周期)
        df["RSI"] = calc_rsi(df["close"], length=RSI_LENGTH)

        # 2. VWAP (成交量加权平均价)
        df["VWAP"] = calc_vwap(df["high"], df["low"], df["close"], df["volume"])

        # 3. VWAP 斜率
        df["VWAP_SLOPE"] = df["VWAP"].diff()

        # 4. 20周期成交量均值
        df["VOL_MA"] = df["volume"].rolling(window=VOL_MA_PERIOD).mean()

        return df
