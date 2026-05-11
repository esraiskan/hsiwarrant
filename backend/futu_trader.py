"""
富途 OpenD 交易执行模块
负责在模拟盘/真实盘下单、查询持仓、查询订单
"""
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from futu import (
    OpenSecTradeContext, RET_OK, TrdEnv, TrdSide,
    OrderType, TrdMarket, SecurityFirm, ModifyOrderOp,
)
from config import FUTU_HOST, FUTU_PORT


TRADE_ENV_STATE_PATH = Path(__file__).with_name("trade_env_state.json")
HK_TZ = timezone(timedelta(hours=8), name="Asia/Hong_Kong")
ORDER_QUERY_WINDOW_SECONDS = 30.0
ORDER_QUERY_MAX_PER_WINDOW = 9
ORDER_QUERY_MIN_INTERVAL_SECONDS = 3.2


def _today_hk() -> str:
    return datetime.now(HK_TZ).date().isoformat()


def _now_hk() -> str:
    return datetime.now(HK_TZ).isoformat(timespec="seconds")


def _normalise_env(trade_env: str) -> str:
    env = (trade_env or "").upper()
    if env not in {"SIMULATE", "REAL"}:
        raise ValueError("交易环境必须是 SIMULATE 或 REAL")
    return env


def _to_trd_env(trade_env: str):
    return TrdEnv.REAL if _normalise_env(trade_env) == "REAL" else TrdEnv.SIMULATE


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, "", "N/A"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _has_number(value) -> bool:
    try:
        if value in (None, "", "N/A"):
            return False
        float(value)
        return True
    except (TypeError, ValueError):
        return False


class FutuTrader:
    """富途交易执行器"""

    def __init__(self):
        self.trade_ctx: OpenSecTradeContext | None = None
        self._connected = False
        self._real_unlocked_date: str | None = None
        self.trd_env = _to_trd_env(self._load_trade_env())
        self._order_cache: dict[str, dict] = {}
        self._order_query_times: dict[str, deque[float]] = {}

    def _load_trade_env(self) -> str:
        """读取当天交易环境；过期或无状态时回到模拟盘。"""
        try:
            if TRADE_ENV_STATE_PATH.exists():
                data = json.loads(TRADE_ENV_STATE_PATH.read_text(encoding="utf-8"))
                env = _normalise_env(str(data.get("trade_env", "SIMULATE")))
                unlocked_date = data.get("real_unlocked_date")
                if env == "REAL" and unlocked_date == _today_hk():
                    self._real_unlocked_date = str(unlocked_date)
                    return "REAL"
        except Exception as e:
            print(f"[FutuTrader] 读取交易环境状态失败: {e}")

        return "SIMULATE"

    def _save_trade_env(self, trade_env: str, real_unlocked_date: str | None = None):
        env = _normalise_env(trade_env)
        payload = {
            "trade_env": env,
            "real_unlocked_date": real_unlocked_date if env == "REAL" else None,
            "updated_at": _now_hk(),
        }
        TRADE_ENV_STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._real_unlocked_date = payload["real_unlocked_date"]

    def get_trade_env(self) -> str:
        return "REAL" if self.trd_env == TrdEnv.REAL else "SIMULATE"

    @property
    def real_unlocked_today(self) -> bool:
        return self._real_unlocked_date == _today_hk()

    @property
    def trade_env_date(self) -> str | None:
        return self._real_unlocked_date

    def set_trade_env(self, trade_env: str, persist: bool = True) -> dict:
        """切换交易环境；调用方负责在切到真实盘前完成解锁。"""
        env = _normalise_env(trade_env)
        old_env = self.get_trade_env()
        if old_env != env and self.is_connected:
            self.disconnect()
        self.trd_env = _to_trd_env(env)
        if persist:
            self._save_trade_env(env, _today_hk() if env == "REAL" else None)
        return {"success": True, "trade_env": env, "message": f"已切换到{'真实盘' if env == 'REAL' else '模拟盘'}"}

    def unlock_real_trade(self, password: str) -> dict:
        """使用当天输入的交易密码解锁真实盘，成功后才切换到真实盘。"""
        if not password:
            return {"success": False, "message": "请输入交易密码", "trade_env": self.get_trade_env()}

        old_env = self.get_trade_env()
        if self.is_connected:
            self.disconnect()
        self.trd_env = TrdEnv.REAL
        if not self.connect():
            self.trd_env = _to_trd_env(old_env)
            return {"success": False, "message": "交易网关未连接", "trade_env": old_env}

        try:
            ret, data = self.trade_ctx.unlock_trade(password=password, is_unlock=True)
            if ret != RET_OK:
                self.disconnect()
                self.trd_env = _to_trd_env(old_env)
                print(f"[FutuTrader] 真实盘解锁失败: {data}")
                return {"success": False, "message": str(data), "trade_env": old_env}
            self._save_trade_env("REAL", _today_hk())
            print("[FutuTrader] 真实盘交易已解锁")
            return {"success": True, "message": "真实盘交易已解锁", "trade_env": "REAL"}
        except Exception as e:
            self.disconnect()
            self.trd_env = _to_trd_env(old_env)
            print(f"[FutuTrader] 真实盘解锁异常: {e}")
            return {"success": False, "message": str(e), "trade_env": old_env}

    def connect(self) -> bool:
        """连接交易网关"""
        if self._connected and self.trade_ctx is not None:
            return True
        try:
            self.trade_ctx = OpenSecTradeContext(
                host=FUTU_HOST,
                port=FUTU_PORT,
                filter_trdmarket=TrdMarket.HK,
                security_firm=SecurityFirm.FUTUSECURITIES,
            )
            self._connected = True
            env_name = "模拟盘" if self.trd_env == TrdEnv.SIMULATE else "真实盘"
            print(f"[FutuTrader] 已连接交易网关 ({env_name})")
            return True
        except Exception as e:
            print(f"[FutuTrader] 连接交易网关失败: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """断开连接"""
        if self.trade_ctx:
            try:
                self.trade_ctx.close()
            except Exception:
                pass
            self.trade_ctx = None
            self._connected = False
            print("[FutuTrader] 已断开交易网关")

    @property
    def is_connected(self) -> bool:
        return self._connected and self.trade_ctx is not None

    def _allow_order_query(self, order_id: str) -> bool:
        now = time.monotonic()
        query_times = self._order_query_times.setdefault(order_id, deque())
        while query_times and now - query_times[0] >= ORDER_QUERY_WINDOW_SECONDS:
            query_times.popleft()
        if query_times and now - query_times[-1] < ORDER_QUERY_MIN_INTERVAL_SECONDS:
            return False
        if len(query_times) >= ORDER_QUERY_MAX_PER_WINDOW:
            return False
        query_times.append(now)
        return True

    def _cached_order(self, order_id: str) -> dict | None:
        cached = self._order_cache.get(order_id)
        return dict(cached) if cached else None

    def _clear_order_query_state(self, order_id: str):
        order_id = str(order_id)
        self._order_cache.pop(order_id, None)
        self._order_query_times.pop(order_id, None)

    def place_order(self, code: str, price: float, qty: int, side: str, order_type=OrderType.NORMAL) -> dict:
        """
        下单
        :param code: 证券代码，如 'HK.61234'
        :param price: 价格
        :param qty: 数量
        :param side: 'BUY' 或 'SELL'
        :return: 订单结果
        """
        if not self.is_connected:
            if not self.connect():
                return {"success": False, "message": "交易网关未连接"}

        trd_side = TrdSide.BUY if side == "BUY" else TrdSide.SELL

        try:
            ret, data = self.trade_ctx.place_order(
                price=price,
                qty=qty,
                code=code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=self.trd_env,
            )
            if ret != RET_OK:
                print(f"[FutuTrader] 下单失败: {data}")
                return {"success": False, "message": str(data)}

            order_id = data["order_id"].iloc[0] if "order_id" in data.columns else "unknown"
            print(f"[FutuTrader] 下单成功: {side} {code} x{qty} @ {price}, 订单号: {order_id}")
            return {
                "success": True,
                "order_id": str(order_id),
                "order_status": str(data["order_status"].iloc[0]) if "order_status" in data.columns else "",
                "price": float(data["price"].iloc[0]) if "price" in data.columns else float(price),
                "qty": float(data["qty"].iloc[0]) if "qty" in data.columns else float(qty),
                "message": f"下单成功: {side} {code} x{qty}",
            }
        except Exception as e:
            print(f"[FutuTrader] 下单异常: {e}")
            return {"success": False, "message": str(e)}

    def modify_order(self, order_id: str, price: float, qty: float) -> dict:
        """修改订单价格和数量。"""
        if not self.is_connected:
            if not self.connect():
                return {"success": False, "message": "交易网关未连接"}
        try:
            ret, data = self.trade_ctx.modify_order(
                ModifyOrderOp.NORMAL,
                order_id=order_id,
                qty=qty,
                price=price,
                trd_env=self.trd_env,
            )
            if ret != RET_OK:
                print(f"[FutuTrader] 改单失败: {data}")
                return {"success": False, "message": str(data)}
            print(f"[FutuTrader] 改单成功: order_id={order_id} qty={qty} price={price}")
            self._clear_order_query_state(order_id)
            return {"success": True, "message": "改单成功"}
        except Exception as e:
            print(f"[FutuTrader] 改单异常: {e}")
            return {"success": False, "message": str(e)}

    def cancel_order(self, order_id: str) -> dict:
        """取消订单。"""
        if not self.is_connected:
            if not self.connect():
                return {"success": False, "message": "交易网关未连接"}
        try:
            ret, data = self.trade_ctx.modify_order(
                ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=self.trd_env,
            )
            if ret != RET_OK:
                print(f"[FutuTrader] 撤单失败: {data}")
                return {"success": False, "message": str(data)}
            print(f"[FutuTrader] 撤单成功: order_id={order_id}")
            self._clear_order_query_state(order_id)
            return {"success": True, "message": "撤单成功"}
        except Exception as e:
            print(f"[FutuTrader] 撤单异常: {e}")
            return {"success": False, "message": str(e)}

    def get_order(self, order_id: str) -> dict | None:
        """查询单张订单。"""
        order_id = str(order_id)
        if not self._allow_order_query(order_id):
            return self._cached_order(order_id)
        if not self.is_connected:
            if not self.connect():
                return None
        try:
            ret, data = self.trade_ctx.order_list_query(
                order_id=order_id,
                trd_env=self.trd_env,
                refresh_cache=True,
            )
            if ret != RET_OK or data is None or data.empty:
                print(f"[FutuTrader] 查询订单失败: {order_id} {data}")
                return self._cached_order(order_id)
            row = data.iloc[0]
            order = {
                "order_id": str(row.get("order_id", "")),
                "code": str(row.get("code", "")),
                "name": str(row.get("stock_name", "")),
                "trd_side": str(row.get("trd_side", "")),
                "order_type": str(row.get("order_type", "")),
                "qty": float(row.get("qty", 0)),
                "price": float(row.get("price", 0)),
                "dealt_qty": float(row.get("dealt_qty", 0)),
                "dealt_avg_price": float(row.get("dealt_avg_price", 0)),
                "order_status": str(row.get("order_status", "")),
                "create_time": str(row.get("create_time", "")),
                "updated_time": str(row.get("updated_time", "")),
            }
            self._order_cache[order_id] = order
            return dict(order)
        except Exception as e:
            print(f"[FutuTrader] 查询订单异常: {e}")
            return self._cached_order(order_id)

    def get_positions(self) -> list[dict]:
        """查询当前持仓"""
        if not self.is_connected:
            if not self.connect():
                return []
        try:
            ret, data = self.trade_ctx.position_list_query(trd_env=self.trd_env)
            if ret != RET_OK:
                print(f"[FutuTrader] 查询持仓失败: {data}")
                return []
            positions = []
            for _, row in data.iterrows():
                positions.append({
                    "code": str(row["code"]),
                    "name": str(row.get("stock_name", "")),
                    "qty": _to_float(row["qty"]),
                    "cost_price": _to_float(row.get("cost_price", 0)),
                    "market_val": _to_float(row.get("market_val", 0)),
                    "pl_val": _to_float(row.get("pl_val", 0)),
                    "pl_ratio": _to_float(row.get("pl_ratio", 0)),
                    "today_pl_val": _to_float(row.get("today_pl_val", 0)),
                    "today_buy_val": _to_float(row.get("today_buy_val", 0)),
                    "today_sell_val": _to_float(row.get("today_sell_val", 0)),
                    "unrealized_pl": _to_float(row.get("unrealized_pl", 0)),
                    "realized_pl": _to_float(row.get("realized_pl", 0)),
                    "position_side": str(row.get("position_side", "")),
                })
            return positions
        except Exception as e:
            print(f"[FutuTrader] 查询持仓异常: {e}")
            return []

    def get_today_pnl(self) -> dict:
        """查询当前交易环境下的今日 P&L。优先使用富途持仓返回的 today_pl_val。"""
        if not self.is_connected:
            if not self.connect():
                return {
                    "success": False,
                    "trade_env": self.get_trade_env(),
                    "date": _today_hk(),
                    "today_pnl_hkd": 0.0,
                    "source": "opend_position_today_pl",
                    "message": "交易网关未连接",
                    "positions": [],
                }

        try:
            ret, data = self.trade_ctx.position_list_query(trd_env=self.trd_env)
            if ret != RET_OK:
                return {
                    "success": False,
                    "trade_env": self.get_trade_env(),
                    "date": _today_hk(),
                    "today_pnl_hkd": 0.0,
                    "source": "opend_position_today_pl",
                    "message": str(data),
                    "positions": [],
                }

            positions = []
            today_pnl = 0.0
            position_pnl = 0.0
            valid_today_pnl_count = 0
            for _, row in data.iterrows():
                raw_today_pnl = row.get("today_pl_val", 0)
                if _has_number(raw_today_pnl):
                    valid_today_pnl_count += 1
                item_today_pnl = _to_float(raw_today_pnl)
                item_position_pnl = _to_float(row.get("pl_val", 0))
                today_pnl += item_today_pnl
                position_pnl += item_position_pnl
                positions.append({
                    "code": str(row.get("code", "")),
                    "name": str(row.get("stock_name", "")),
                    "qty": _to_float(row.get("qty", 0)),
                    "market_val": _to_float(row.get("market_val", 0)),
                    "today_pl_val": round(item_today_pnl, 2),
                    "pl_val": round(item_position_pnl, 2),
                    "unrealized_pl": _to_float(row.get("unrealized_pl", 0)),
                    "realized_pl": _to_float(row.get("realized_pl", 0)),
                    "position_side": str(row.get("position_side", "")),
                })

            if round(today_pnl, 2) == 0 and round(position_pnl, 2) != 0:
                return {
                    "success": True,
                    "trade_env": self.get_trade_env(),
                    "date": _today_hk(),
                    "today_pnl_hkd": round(position_pnl, 2),
                    "source": "opend_position_pl_val",
                    "message": "OpenD 今日 P&L 为 0，已使用持仓 P&L",
                    "positions": positions,
                }

            if not positions or valid_today_pnl_count == 0:
                estimated = self._estimate_today_pnl_from_deals()
                if estimated.get("success"):
                    return estimated

            return {
                "success": True,
                "trade_env": self.get_trade_env(),
                "date": _today_hk(),
                "today_pnl_hkd": round(today_pnl, 2),
                "source": "opend_position_today_pl",
                "message": "",
                "positions": positions,
            }
        except Exception as e:
            print(f"[FutuTrader] 查询今日 P&L 异常: {e}")
            return {
                "success": False,
                "trade_env": self.get_trade_env(),
                "date": _today_hk(),
                "today_pnl_hkd": 0.0,
                "source": "opend_position_today_pl",
                "message": str(e),
                "positions": [],
            }

    def _estimate_today_pnl_from_deals(self) -> dict:
        """用今日成交估算同日买卖已实现 P&L。仅作为 OpenD 持仓今日 P&L 不可用时的后备。"""
        try:
            ret, data = self.trade_ctx.deal_list_query(
                trd_env=self.trd_env,
                refresh_cache=True,
            )
            if ret != RET_OK:
                return {
                    "success": False,
                    "trade_env": self.get_trade_env(),
                    "date": _today_hk(),
                    "today_pnl_hkd": 0.0,
                    "source": "opend_deal_fifo_estimate",
                    "message": str(data),
                    "positions": [],
                }

            lots_by_code: dict[str, list[list[float]]] = {}
            realized = 0.0
            deals = []
            for _, row in data.iterrows():
                deals.append({
                    "code": str(row.get("code", "")),
                    "side": str(row.get("trd_side", "")).upper(),
                    "qty": _to_float(row.get("qty", 0)),
                    "price": _to_float(row.get("price", 0)),
                    "time": str(row.get("create_time", "")),
                })

            for deal in sorted(deals, key=lambda item: item["time"]):
                code = deal["code"]
                qty = deal["qty"]
                price = deal["price"]
                if not code or qty <= 0 or price <= 0:
                    continue
                if "BUY" in deal["side"]:
                    lots_by_code.setdefault(code, []).append([qty, price])
                    continue
                if "SELL" not in deal["side"]:
                    continue

                remaining = qty
                lots = lots_by_code.setdefault(code, [])
                while remaining > 0 and lots:
                    lot_qty, lot_price = lots[0]
                    matched_qty = min(remaining, lot_qty)
                    realized += (price - lot_price) * matched_qty
                    remaining -= matched_qty
                    lot_qty -= matched_qty
                    if lot_qty <= 0:
                        lots.pop(0)
                    else:
                        lots[0][0] = lot_qty

            return {
                "success": True,
                "trade_env": self.get_trade_env(),
                "date": _today_hk(),
                "today_pnl_hkd": round(realized, 2),
                "source": "opend_deal_fifo_estimate",
                "message": "OpenD 持仓今日 P&L 暂无数据，已用今日成交估算，不含费用",
                "positions": [],
            }
        except Exception as e:
            print(f"[FutuTrader] 估算今日 P&L 异常: {e}")
            return {
                "success": False,
                "trade_env": self.get_trade_env(),
                "date": _today_hk(),
                "today_pnl_hkd": 0.0,
                "source": "opend_deal_fifo_estimate",
                "message": str(e),
                "positions": [],
            }

    def get_orders(self) -> list[dict]:
        """查询今日订单"""
        if not self.is_connected:
            if not self.connect():
                return []
        try:
            ret, data = self.trade_ctx.order_list_query(trd_env=self.trd_env)
            if ret != RET_OK:
                return []
            orders = []
            for _, row in data.iterrows():
                orders.append({
                    "order_id": str(row.get("order_id", "")),
                    "code": str(row["code"]),
                    "name": str(row.get("stock_name", "")),
                    "trd_side": str(row.get("trd_side", "")),
                    "order_type": str(row.get("order_type", "")),
                    "qty": float(row.get("qty", 0)),
                    "price": float(row.get("price", 0)),
                    "dealt_qty": float(row.get("dealt_qty", 0)),
                    "dealt_avg_price": float(row.get("dealt_avg_price", 0)),
                    "order_status": str(row.get("order_status", "")),
                    "create_time": str(row.get("create_time", "")),
                })
            return orders
        except Exception as e:
            print(f"[FutuTrader] 查询订单异常: {e}")
            return []

    def get_account_info(self) -> dict | None:
        """查询账户资金"""
        if not self.is_connected:
            if not self.connect():
                return None
        try:
            ret, data = self.trade_ctx.accinfo_query(trd_env=self.trd_env)
            if ret != RET_OK:
                return None
            row = data.iloc[0]
            return {
                "total_assets": float(row.get("total_assets", 0)),
                "cash": float(row.get("cash", 0)),
                "market_val": float(row.get("market_val", 0)),
                "frozen_cash": float(row.get("frozen_cash", 0)),
                "available_funds": float(row.get("available_funds", 0)) if row.get("available_funds") != "N/A" else 0.0,
            }
        except Exception as e:
            print(f"[FutuTrader] 查询账户异常: {e}")
            return None
