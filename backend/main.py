"""
恒指牛熊证智能交易系统 - FastAPI 后端 (OpenD 实盘版)
提供 REST API + WebSocket 实时推送
"""
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import (
    SYMBOL, ER_RATIO, SHARE_COUNT, TARGET_PNL, STOP_POINTS,
    RSI_OVERSOLD, RSI_OVERBOUGHT, VOL_MA_PERIOD, POLL_INTERVAL,
    FUTU_HOST, FUTU_PORT,
)
from models import ConfigResponse, ConfigUpdate, TradeEnvUpdate, TradeEnvUpdateResponse
from strategy import HSIStrategyEngine
from trade_log_store import load_today_trade_log


# ============== WebSocket 连接管理 ==============
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict[str, Any]):
        data = json.dumps(message, ensure_ascii=False, default=str)
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(data)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()
engine = HSIStrategyEngine()


# ============== 回调函数 ==============
async def on_kline_update(kline_data):
    await manager.broadcast({"type": "kline", "data": kline_data.model_dump()})


async def on_kline_batch(kline_list):
    await manager.broadcast({
        "type": "kline_batch",
        "data": [k.model_dump() for k in kline_list],
    })


async def on_trade_signal(trade_record):
    await manager.broadcast({"type": "trade", "data": trade_record.model_dump()})


async def on_state_update(state):
    await manager.broadcast({"type": "state", "data": state.model_dump()})


engine.on_kline_update = on_kline_update
engine.on_kline_batch = on_kline_batch
engine.on_trade_signal = on_trade_signal
engine.on_state_update = on_state_update


# ============== FastAPI 应用 ==============
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.stop()


app = FastAPI(
    title="恒指牛熊证智能交易系统 (OpenD)",
    description="HSI CBBC Algo Trading System - Connected to FuTu OpenD",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============== REST API ==============
def _get_warrant_name_sync(code: str) -> str:
    if not code:
        return ""
    try:
        snapshot = engine.data_source.get_security_snapshot(code)
        if snapshot is None:
            return "未能获取名称"
        return snapshot.get("name") or "未能获取名称"
    except Exception:
        return "未能获取名称"


@app.get("/api/config", response_model=ConfigResponse)
async def get_config():
    loop = asyncio.get_running_loop()
    bull_name, bear_name = await asyncio.gather(
        loop.run_in_executor(None, _get_warrant_name_sync, engine.bull_warrant_code),
        loop.run_in_executor(None, _get_warrant_name_sync, engine.bear_warrant_code),
    )
    return ConfigResponse(
        symbol=SYMBOL,
        er_ratio=engine.er_ratio,
        share_count=engine.share_count,
        target_pnl=engine.target_pnl,
        stop_points=engine.stop_points,
        extreme_stop_pnl=engine.extreme_stop_pnl,
        extreme_stop_points=round(engine._extreme_stop_points(), 2),
        bull_warrant_code=engine.bull_warrant_code,
        bull_warrant_name=bull_name,
        bear_warrant_code=engine.bear_warrant_code,
        bear_warrant_name=bear_name,
        rsi_oversold=engine.rsi_oversold,
        rsi_overbought=engine.rsi_overbought,
        vol_ma_period=VOL_MA_PERIOD,
        poll_interval=engine.poll_interval,
        entry_order_wait_seconds=engine.entry_order_wait_seconds,
        entry_cutoff_time=engine.entry_cutoff_time,
    )


@app.put("/api/config")
async def update_config(config: ConfigUpdate):
    engine.update_config(**config.model_dump(exclude_none=True))
    return {
        "message": "配置已更新",
        "stop_points": engine.stop_points,
        "extreme_stop_points": round(engine._extreme_stop_points(), 2),
    }


@app.get("/api/state")
async def get_state():
    loop = asyncio.get_running_loop()
    state = await loop.run_in_executor(None, engine.get_state)
    return state.model_dump()


@app.post("/api/start")
async def start_strategy():
    if engine.is_running:
        return {"message": "策略已在运行中"}
    try:
        await engine.start()
        return {"message": "策略已启动，已连接 OpenD"}
    except RuntimeError as e:
        return {"message": f"启动失败: {str(e)}"}


@app.post("/api/stop")
async def stop_strategy():
    if not engine.is_running:
        return {"message": "策略未在运行"}
    await engine.stop()
    return {"message": "策略已停止"}


@app.post("/api/reset")
async def reset_strategy():
    if engine.is_running:
        await engine.stop()
    engine.reset()
    engine.on_kline_update = on_kline_update
    engine.on_kline_batch = on_kline_batch
    engine.on_trade_signal = on_trade_signal
    engine.on_state_update = on_state_update
    return {"message": "策略已重置"}


@app.get("/api/trades")
async def get_trades():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, engine.sync_exit_order_if_filled)
    return [t.model_dump() for t in load_today_trade_log()]


@app.get("/api/klines")
async def get_klines():
    return [k.model_dump() for k in engine.kline_history_1m]


# ============== OpenD 相关 API ==============
@app.get("/api/opend/status")
async def get_opend_status():
    """获取 OpenD 连接状态"""
    return {
        "host": FUTU_HOST,
        "port": FUTU_PORT,
        "trade_env": engine.trader.get_trade_env(),
        "real_unlocked_today": engine.trader.real_unlocked_today,
        "trade_env_date": engine.trader.trade_env_date,
        "quote_connected": engine.data_source.is_connected,
        "trade_connected": engine.trader.is_connected,
        "strategy_running": engine.is_running,
    }


@app.post("/api/opend/trade-env", response_model=TradeEnvUpdateResponse)
async def update_trade_env(payload: TradeEnvUpdate):
    """切换交易环境；切到真实盘时使用当天输入的密码解锁。"""
    loop = asyncio.get_event_loop()
    if payload.trade_env == "REAL":
        result = await loop.run_in_executor(
            None,
            engine.trader.unlock_real_trade,
            payload.trade_password or "",
        )
    else:
        result = await loop.run_in_executor(None, engine.trader.set_trade_env, "SIMULATE")

    return TradeEnvUpdateResponse(
        success=bool(result.get("success")),
        message=str(result.get("message", "")),
        trade_env=engine.trader.get_trade_env(),
        real_unlocked_today=engine.trader.real_unlocked_today,
        trade_env_date=engine.trader.trade_env_date,
    )


@app.get("/api/opend/snapshot")
async def get_snapshot():
    """获取恒指实时快照"""
    loop = asyncio.get_event_loop()
    # 临时连接获取快照
    if not engine.data_source.is_connected:
        engine.data_source.connect()
    snapshot = await loop.run_in_executor(None, engine.data_source.get_snapshot)
    if not engine.is_running:
        engine.data_source.disconnect()
    if snapshot is None:
        return {"error": "无法获取行情快照，请确认 OpenD 已启动"}
    return snapshot


@app.get("/api/opend/account")
async def get_account():
    """获取模拟盘账户信息"""
    if not engine.trader.is_connected:
        engine.trader.connect()
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, engine.trader.get_account_info)
    if not engine.is_running:
        engine.trader.disconnect()
    if info is None:
        return {"error": "无法获取账户信息"}
    return info


@app.get("/api/opend/positions")
async def get_positions():
    """获取模拟盘持仓"""
    if not engine.trader.is_connected:
        engine.trader.connect()
    loop = asyncio.get_event_loop()
    positions = await loop.run_in_executor(None, engine.trader.get_positions)
    if not engine.is_running:
        engine.trader.disconnect()
    return positions


@app.get("/api/opend/today-pnl")
async def get_today_pnl():
    """获取当前交易环境下当天 P&L。"""
    if not engine.trader.is_connected:
        engine.trader.connect()
    loop = asyncio.get_event_loop()
    pnl = await loop.run_in_executor(None, engine.trader.get_today_pnl)
    if not engine.is_running:
        engine.trader.disconnect()
    if not pnl.get("success"):
        pnl["fallback_pnl_hkd"] = round(engine.total_pnl_hkd, 2)
    return pnl


@app.get("/api/opend/orders")
async def get_orders():
    """获取模拟盘今日订单"""
    if not engine.trader.is_connected:
        engine.trader.connect()
    loop = asyncio.get_event_loop()
    orders = await loop.run_in_executor(None, engine.trader.get_orders)
    if not engine.is_running:
        engine.trader.disconnect()
    return orders


# ============== WebSocket ==============
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, engine.get_state)
        await websocket.send_text(json.dumps({
            "type": "state",
            "data": state.model_dump(),
        }, ensure_ascii=False, default=str))

        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=6000)
