"""
富途 OpenD 行情数据源 - 全推送驱动
- StockQuoteHandlerBase: 实时报价推送 → 状态面板价格跳动
- CurKlineHandlerBase:   实时K线推送 → 图表实时更新
- get_cur_kline:          策略研判拉取完整K线计算指标
"""
import pandas as pd
import numpy as np
import math
from typing import Callable, Optional
from futu import (
    OpenQuoteContext, RET_OK, KLType, AuType, SubType,
    StockQuoteHandlerBase, CurKlineHandlerBase,
)
from config import FUTU_HOST, FUTU_PORT, SYMBOL, RSI_LENGTH, VOL_MA_PERIOD


def calc_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    typical_price = (high + low + close) / 3.0
    if volume.sum() == 0:
        return typical_price.expanding().mean()
    return (typical_price * volume).cumsum() / volume.cumsum()


def _safe_float(value) -> float | None:
    try:
        if value in (None, "N/A", ""):
            return None
        result = float(value)
        if math.isnan(result) or result <= 0:
            return None
        return result
    except (TypeError, ValueError):
        return None


# ============== 推送处理器 ==============

class _QuotePushHandler(StockQuoteHandlerBase):
    """实时报价推送 → 价格跳动"""

    def __init__(self):
        super().__init__()
        self.callback: Optional[Callable] = None

    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret == RET_OK and self.callback:
            for _, row in data.iterrows():
                if row["code"] == SYMBOL:
                    self.callback({
                        "last_price": float(row["last_price"]),
                        "open_price": float(row.get("open_price", 0)),
                        "high_price": float(row.get("high_price", 0)),
                        "low_price": float(row.get("low_price", 0)),
                        "turnover": float(row.get("turnover", 0)),
                        "time": str(row.get("data_time", "")),
                    })
        return RET_OK, data


class _KlinePushHandler(CurKlineHandlerBase):
    """实时 K 线推送 → 图表更新"""

    def __init__(self):
        super().__init__()
        self.callback: Optional[Callable] = None

    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret == RET_OK and self.callback:
            for _, row in data.iterrows():
                if row["code"] == SYMBOL:
                    self.callback({
                        "time": str(row["time_key"]),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0)),
                        "turnover": float(row.get("turnover", 0)),
                        "k_type": str(row.get("k_type", "")),
                    })
        return RET_OK, data


# ============== 数据源 ==============

class FutuDataSource:

    def __init__(self):
        self.quote_ctx: OpenQuoteContext | None = None
        self._connected = False
        self._subscribed = False
        self._quote_handler = _QuotePushHandler()
        self._kline_handler = _KlinePushHandler()

        # 外部回调
        self.on_price_push: Optional[Callable] = None   # 报价推送
        self.on_kline_push: Optional[Callable] = None   # K线推送

    def connect(self):
        if self._connected and self.quote_ctx is not None:
            return True
        try:
            self.quote_ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
            # 绑定推送处理器
            self._quote_handler.callback = self._on_quote_push
            self._kline_handler.callback = self._on_kline_push
            self.quote_ctx.set_handler(self._quote_handler)
            self.quote_ctx.set_handler(self._kline_handler)
            self._connected = True
            self._subscribed = False
            print(f"[FutuData] 已连接 OpenD @ {FUTU_HOST}:{FUTU_PORT}")
            return True
        except Exception as e:
            print(f"[FutuData] 连接 OpenD 失败: {e}")
            self._connected = False
            return False

    def _on_quote_push(self, data: dict):
        if self.on_price_push:
            self.on_price_push(data)

    def _on_kline_push(self, data: dict):
        if self.on_kline_push:
            self.on_kline_push(data)

    def _ensure_subscribed(self):
        if self._subscribed or not self.quote_ctx:
            return
        try:
            ret, msg = self.quote_ctx.subscribe(
                [SYMBOL],
                [SubType.QUOTE, SubType.K_1M, SubType.K_15M],
            )
            if ret == RET_OK:
                self._subscribed = True
                print(f"[FutuData] 已订阅 {SYMBOL} (报价 + 1M + 15M)")
            else:
                print(f"[FutuData] 订阅失败: {msg}")
        except Exception as e:
            print(f"[FutuData] 订阅异常: {e}")

    def disconnect(self):
        if self.quote_ctx:
            try:
                self.quote_ctx.close()
            except Exception:
                pass
            self.quote_ctx = None
            self._connected = False
            self._subscribed = False
            print("[FutuData] 已断开 OpenD 连接")

    @property
    def is_connected(self) -> bool:
        return self._connected and self.quote_ctx is not None

    def get_realtime_price(self) -> dict | None:
        if not self.is_connected:
            if not self.connect():
                return None
        try:
            ret, data = self.quote_ctx.get_market_snapshot([SYMBOL])
            if ret != RET_OK:
                return None
            row = data.iloc[0]
            return {
                "code": str(row["code"]),
                "name": str(row.get("name", SYMBOL)),
                "last_price": float(row["last_price"]),
                "open_price": float(row["open_price"]),
                "high_price": float(row["high_price"]),
                "low_price": float(row["low_price"]),
                "prev_close": float(row.get("prev_close_price", 0)),
                "volume": int(row.get("volume", 0)),
                "turnover": float(row.get("turnover", 0)),
                "update_time": str(row.get("update_time", "")),
            }
        except Exception as e:
            print(f"[FutuData] 获取实时价格异常: {e}")
            return None

    def get_kline_with_indicators(self, ktype: str = "1m") -> pd.DataFrame | None:
        """拉取实时 K 线并计算指标 (策略研判用)"""
        if not self.is_connected:
            if not self.connect():
                return None
        self._ensure_subscribed()

        ktype_map = {"1m": KLType.K_1M, "15m": KLType.K_15M, "30m": KLType.K_30M}
        futu_ktype = ktype_map.get(ktype, KLType.K_1M)
        num = 100 if ktype == "1m" else 50

        try:
            ret, data = self.quote_ctx.get_cur_kline(
                SYMBOL, num, ktype=futu_ktype, autype=AuType.QFQ,
            )
            if ret != RET_OK:
                print(f"[FutuData] 实时K线获取失败: {data}")
                return None

            df = data.copy()
            df["time_key"] = pd.to_datetime(df["time_key"])
            df.set_index("time_key", inplace=True)
            if len(df) < 2:
                return None

            df["RSI"] = calc_rsi(df["close"], length=RSI_LENGTH)
            vol_series = df["turnover"] if "turnover" in df.columns and df["turnover"].sum() > 0 else df["volume"]
            df["VWAP"] = calc_vwap(df["high"], df["low"], df["close"], vol_series)
            df["VWAP_SLOPE"] = df["VWAP"].diff()
            df["volume"] = vol_series
            df["VOL_MA"] = vol_series.rolling(window=VOL_MA_PERIOD).mean()
            return df
        except Exception as e:
            print(f"[FutuData] 获取实时K线异常: {e}")
            self._connected = False
            return None

    def get_snapshot(self) -> dict | None:
        return self.get_realtime_price()

    def get_security_snapshot(self, code: str) -> dict | None:
        """获取指定牛熊证/证券快照，含买一、卖一和最小价差。"""
        if not self.is_connected:
            if not self.connect():
                return None
        try:
            ret, data = self.quote_ctx.get_market_snapshot([code])
            if ret != RET_OK or data is None or data.empty:
                print(f"[FutuData] 获取 {code} 快照失败: {data}")
                return None

            row = data.iloc[0]
            bid_price = _safe_float(row.get("bid_price"))
            ask_price = _safe_float(row.get("ask_price"))
            price_spread = _safe_float(row.get("price_spread"))
            last_price = _safe_float(row.get("last_price"))
            lot_size = _safe_float(row.get("lot_size"))
            if bid_price is None or ask_price is None or price_spread is None:
                print(
                    f"[FutuData] {code} 买卖价无效: "
                    f"bid={row.get('bid_price')} ask={row.get('ask_price')} spread={row.get('price_spread')}"
                )
                return None

            return {
                "code": str(row["code"]),
                "name": str(row.get("name", code)),
                "bid_price": bid_price,
                "ask_price": ask_price,
                "price_spread": price_spread,
                "lot_size": int(lot_size) if lot_size is not None else 0,
                "last_price": last_price or 0.0,
                "update_time": str(row.get("update_time", "")),
            }
        except Exception as e:
            print(f"[FutuData] 获取 {code} 快照异常: {e}")
            return None

    def get_market_breadth(self) -> dict | None:
        """获取恒指涨跌家数比 + 振幅"""
        if not self.is_connected:
            if not self.connect():
                return None
        try:
            ret, data = self.quote_ctx.get_market_snapshot([SYMBOL])
            if ret != RET_OK:
                return None
            row = data.iloc[0]
            return {
                "raise_count": int(row.get("index_raise_count", 0)),
                "fall_count": int(row.get("index_fall_count", 0)),
                "equal_count": int(row.get("index_equal_count", 0)),
                "amplitude": float(row.get("amplitude", 0)),
                "open_price": float(row.get("open_price", 0)),
                "high_price": float(row.get("high_price", 0)),
                "low_price": float(row.get("low_price", 0)),
                "last_price": float(row.get("last_price", 0)),
                "time": str(row.get("update_time", "")),
            }
        except Exception as e:
            print(f"[FutuData] 获取涨跌家数异常: {e}")
            return None
