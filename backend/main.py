"""
恒指牛熊证智能交易系统 - FastAPI 后端 (OpenD 实盘版)
提供 REST API + WebSocket 实时推送
"""
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import (
    SYMBOL, ER_RATIO, SHARE_COUNT, TARGET_PNL, STOP_POINTS,
    RSI_OVERSOLD, RSI_OVERBOUGHT, VOL_MA_PERIOD, POLL_INTERVAL,
    FUTU_HOST, FUTU_PORT,
)
from backtest_service import run_backtest
from models import (
    BacktestRequest,
    BacktestResult,
    CbbcAiAdviceResponse,
    HksiStyleAiAdviceResponse,
    CbbcZoneCluster,
    CbbcZonesResponse,
    ConfigResponse,
    ConfigUpdate,
    MagnetOverlayPayload,
    MagnetOverlayCallLevel,
    MagnetOverlayHistogramBucket,
    MagnetOverlayVeto,
    TradeEnvUpdate,
    TradeEnvUpdateResponse,
)
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


async def on_market_regime_update(regime):
    await manager.broadcast({"type": "market_regime", "data": regime.model_dump()})


# --------------------------------------------------------------------------- #
# CBBC magnet overlay (cbbc-magnet-signal task 9.2)
# --------------------------------------------------------------------------- #
# Recent vetoes are tracked in-memory and forwarded on every magnet_overlay
# push so the frontend can render the markers (R8.5). Trimmed to the last 50
# events to keep the payload small.
_RECENT_VETOES: list[MagnetOverlayVeto] = []
_RECENT_VETOES_MAX = 50


def _record_magnet_veto(consult) -> None:
    """Push a veto event onto the rolling buffer (called from on_trade_signal)."""
    if consult is None or not getattr(consult, "vetoed_by_cbbc_magnet", False):
        return
    try:
        veto = MagnetOverlayVeto(
            kline_time=str(getattr(consult, "ts_hk", "")),
            direction=str(getattr(consult, "extreme_direction", "BULL")),
            reason_code=str(getattr(consult, "reason_code", "")),
        )
    except Exception:  # noqa: BLE001
        return
    _RECENT_VETOES.append(veto)
    if len(_RECENT_VETOES) > _RECENT_VETOES_MAX:
        del _RECENT_VETOES[: len(_RECENT_VETOES) - _RECENT_VETOES_MAX]


def _build_magnet_overlay_payload() -> MagnetOverlayPayload:
    """Compose the WS overlay payload from engine state + runtime config."""
    decay_points = float(engine.cbbc_magnet_decay_points)
    dense_share = float(engine.cbbc_dense_band_pull_share)
    degraded = bool(engine._cbbc_magnet_is_degraded())

    # Read the latest magnet result through the engine helpers (they wrap
    # exceptions and return safe defaults).
    result = engine._cbbc_latest_result()
    snapshot = engine._cbbc_data_service_snapshot() if hasattr(engine, "_cbbc_data_service_snapshot") else None

    call_levels: list[MagnetOverlayCallLevel] = []
    # 当日 HSI 最高 / 最低 — 用作 B 兜底过滤,把已被吃过的合约从图上抹掉。
    today_low: float | None = None
    today_high: float | None = None
    eng = getattr(engine, "_cbbc_magnet_engine", None)
    if eng is not None and hasattr(eng, "get_today_extremes"):
        try:
            today_low, today_high = eng.get_today_extremes()
        except Exception:  # noqa: BLE001
            pass
    if snapshot is not None and result is not None and not result.hsi_spot_stale:
        spot = float(result.hsi_spot)
        for rec in snapshot.records:
            # 零街货合约 (HKEX 上常见已赎回 / 未售出占位) 不应出现在前端图上 —
            # 它们对市场没有真实拉力,只会让密集带视觉上更密集。
            try:
                outstanding = float(rec.outstanding_shares)
            except Exception:  # noqa: BLE001
                continue
            if outstanding <= 0.0:
                continue
            # B 兜底:已被吃过的合约 (call_level 已落在今天 HSI 高/低区间内)
            # 从图上抹掉,与 ``compute_magnet`` 的过滤行为一致。
            try:
                cl = float(rec.call_level)
            except Exception:  # noqa: BLE001
                continue
            if (
                rec.direction == "bull"
                and today_low is not None
                and cl >= today_low
            ):
                continue
            if (
                rec.direction == "bear"
                and today_high is not None
                and cl <= today_high
            ):
                continue
            try:
                distance = abs(cl - spot)
            except Exception:  # noqa: BLE001
                continue
            if distance > decay_points:
                continue  # outside the visible band (R8.2)
            try:
                call_levels.append(
                    MagnetOverlayCallLevel(
                        code=str(rec.code),
                        direction=str(rec.direction),
                        call_level=cl,
                        issuer=str(rec.issuer) if getattr(rec, "issuer", None) else None,
                        outstanding_shares=outstanding,
                        distance_pts=round(distance, 2),
                    )
                )
            except Exception:  # noqa: BLE001
                continue

    histogram: list[MagnetOverlayHistogramBucket] = []
    if result is not None and not result.hsi_spot_stale:
        for bucket in result.histogram:
            try:
                histogram.append(
                    MagnetOverlayHistogramBucket(
                        bucket_low=float(bucket.bucket_low),
                        bucket_high=float(bucket.bucket_high),
                        pull_hkd=float(bucket.pull_hkd),
                    )
                )
            except Exception:  # noqa: BLE001
                continue

    hsi_stale = bool(getattr(result, "hsi_spot_stale", False)) if result is not None else False

    return MagnetOverlayPayload(
        decay_points=decay_points if engine.cbbc_magnet_layer_enabled and not degraded else None,
        dense_band_pull_share=dense_share,
        dense_band_threshold_pts=float(engine.cbbc_dense_band_threshold_pts),
        cbbc_magnet_degraded=degraded,
        hsi_spot_stale=hsi_stale,
        call_levels=call_levels,
        histogram=histogram,
        recent_vetoes=list(_RECENT_VETOES),
    )


async def broadcast_magnet_overlay() -> None:
    """Push a fresh ``magnet_overlay`` message to all WS clients."""
    try:
        payload = _build_magnet_overlay_payload()
    except Exception:  # noqa: BLE001
        return
    await manager.broadcast({"type": "magnet_overlay", "data": payload.model_dump()})


# Wrap the existing on_state_update to also push a magnet overlay on each
# state tick. Direct attribute mutation keeps the existing on_state_update
# semantics intact.
_previous_on_state_update = on_state_update


async def on_state_update_with_overlay(state):  # noqa: D401 - thin wrapper
    await _previous_on_state_update(state)
    await broadcast_magnet_overlay()
    await broadcast_cbbc_zones()


_previous_on_trade_signal = on_trade_signal


async def on_trade_signal_with_overlay(trade_record):
    await _previous_on_trade_signal(trade_record)
    # When the trade record represents a magnet-veto skip, harvest the
    # consult info from the engine and refresh the overlay so the frontend
    # can place the marker.
    last_consult = getattr(engine, "_last_magnet_consult", None)
    if last_consult is not None:
        _record_magnet_veto(last_consult)
        await broadcast_magnet_overlay()


engine.on_kline_update = on_kline_update
engine.on_kline_batch = on_kline_batch
engine.on_trade_signal = on_trade_signal_with_overlay
engine.on_state_update = on_state_update_with_overlay
engine.on_market_regime_update = on_market_regime_update


# ============== FastAPI 应用 ==============
@asynccontextmanager
async def lifespan(app: FastAPI):
    # cbbc-magnet-signal task 9.1: schedule the CBBC data service alongside
    # the FastAPI lifecycle so the magnet engine receives snapshots even when
    # the strategy itself is not running.
    try:
        await engine.start_cbbc_data_service()
    except Exception as exc:  # noqa: BLE001
        print(f"[CBBC] data service start failed in lifespan: {exc!r}")
    try:
        yield
    finally:
        try:
            await engine.stop_cbbc_data_service()
        except Exception as exc:  # noqa: BLE001
            print(f"[CBBC] data service stop failed in lifespan: {exc!r}")
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
        rsi_length=engine.rsi_length,
        rsi_oversold=engine.rsi_oversold,
        rsi_overbought=engine.rsi_overbought,
        vol_ma_period=VOL_MA_PERIOD,
        poll_interval=engine.poll_interval,
        entry_order_wait_seconds=engine.entry_order_wait_seconds,
        entry_cutoff_time=engine.entry_cutoff_time,
        only_extreme_entries=engine.only_extreme_entries,
        enabled_strategies=engine.enabled_strategies,
        enabled_extreme_branches=engine.enabled_extreme_branches,
        extreme_rsi_stop_veto_enabled=engine.extreme_rsi_stop_veto_enabled,
        extreme_rsi_stop_hard_ticks=engine.extreme_rsi_stop_hard_ticks,
        extreme_rsi_stop_rearm_ticks=engine.extreme_rsi_stop_rearm_ticks,
        # CBBC magnet signal layer (cbbc-magnet-signal R9.1)
        cbbc_magnet_layer_enabled=bool(engine.cbbc_magnet_layer_enabled),
        cbbc_intraday_polling_suspended=bool(engine.cbbc_intraday_polling_suspended),
        cbbc_magnet_decay_points=float(engine.cbbc_magnet_decay_points),
        cbbc_dense_band_threshold_pts=float(engine.cbbc_dense_band_threshold_pts),
        cbbc_dense_band_pull_share=float(engine.cbbc_dense_band_pull_share),
        cbbc_intraday_poll_interval_seconds=float(
            engine.cbbc_intraday_poll_interval_seconds
        ),
        cbbc_magnet_direction_gate_enabled=bool(
            engine.cbbc_magnet_direction_gate_enabled
        ),
        cbbc_magnet_direction_gate_threshold=float(
            engine.cbbc_magnet_direction_gate_threshold
        ),
        cbbc_ai_advisor_enabled=bool(engine.cbbc_ai_advisor_enabled),
        cbbc_ai_advisor_base_url=str(engine.cbbc_ai_advisor_base_url),
        cbbc_ai_advisor_model=str(engine.cbbc_ai_advisor_model),
        cbbc_ai_advisor_api_key=str(engine.cbbc_ai_advisor_api_key),
        cbbc_ai_advisor_api_style=str(engine.cbbc_ai_advisor_api_style),
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
    engine.on_trade_signal = on_trade_signal_with_overlay
    engine.on_state_update = on_state_update_with_overlay
    engine.on_market_regime_update = on_market_regime_update
    return {"message": "策略已重置"}


@app.get("/api/trades")
async def get_trades():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, engine.sync_exit_order_if_filled)
    return [t.model_dump() for t in load_today_trade_log()]


@app.get("/api/klines")
async def get_klines():
    return [k.model_dump() for k in engine.kline_history_1m]


@app.get("/api/market-regime")
async def get_market_regime():
    if engine.market_regime is None:
        return None
    return engine.market_regime.model_dump()


@app.post("/api/backtest", response_model=BacktestResult)
async def run_backtest_api(payload: BacktestRequest):
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, run_backtest, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"回测失败: {e}")


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


@app.post("/api/cbbc/today-extremes")
async def set_today_extremes(payload: dict[str, Any]):
    """手动 seed / 覆盖今天的 HSI 高低,用作 ``compute_magnet`` 的兜底 kill 过滤输入。

    Body: ``{"today_low": 25431.17, "today_high": 25750.0}``
    任一字段省略或 null 时只更新另一个。

    用途:策略启动晚于开盘,内部自动追踪的 high/low 抓不到早盘极值时手动校正。
    """
    eng = getattr(engine, "_cbbc_magnet_engine", None)
    if eng is None or not hasattr(eng, "set_today_extremes"):
        raise HTTPException(status_code=503, detail="magnet engine unavailable")
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Zi
        today_hk = _dt.now(_Zi("Asia/Hong_Kong")).date()
        # Read current values so partial updates merge.
        cur_low, cur_high = eng.get_today_extremes()
        new_low = payload.get("today_low", cur_low)
        new_high = payload.get("today_high", cur_high)
        eng.set_today_extremes(
            today_low=float(new_low) if new_low is not None else None,
            today_high=float(new_high) if new_high is not None else None,
            for_date=today_hk,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid payload: {exc!r}")
    low, high = eng.get_today_extremes()
    return {"today_low": low, "today_high": high}


@app.get("/api/cbbc/today-extremes")
async def get_today_extremes():
    eng = getattr(engine, "_cbbc_magnet_engine", None)
    if eng is None or not hasattr(eng, "get_today_extremes"):
        return {"today_low": None, "today_high": None}
    low, high = eng.get_today_extremes()
    return {"today_low": low, "today_high": high}


# --------------------------------------------------------------------------- #
# CBBC 街货密集区 (read-only;不参与交易决策,纯展示)
# --------------------------------------------------------------------------- #


def _build_cbbc_zones_response() -> CbbcZonesResponse:
    """从 magnet engine 当前持有的 snapshot + today low/high 算出目标位 / 支撑位。"""
    from cbbc_zones import compute_zones  # local import — pure helper, no side effects
    from models import CbbcTradeSetup as _CbbcTradeSetupModel

    snapshot = engine._cbbc_data_service_snapshot() if hasattr(engine, "_cbbc_data_service_snapshot") else None
    eng = getattr(engine, "_cbbc_magnet_engine", None)
    today_low = today_high = None
    if eng is not None and hasattr(eng, "get_today_extremes"):
        try:
            today_low, today_high = eng.get_today_extremes()
        except Exception:  # noqa: BLE001
            pass
    spot = float(engine.current_price) if engine.current_price else 0.0

    payload = compute_zones(
        snapshot,
        spot,
        today_low=today_low,
        today_high=today_high,
    )

    def _setup_to_model(setup):  # type: ignore[no-untyped-def]
        if setup is None:
            return None
        return _CbbcTradeSetupModel(
            direction=setup.direction,
            entry_low=setup.entry_low,
            entry_high=setup.entry_high,
            take_profit_1=setup.take_profit_1,
            take_profit_2=setup.take_profit_2,
            stop_loss=setup.stop_loss,
            risk_reward=setup.risk_reward,
            rationale=setup.rationale,
        )

    return CbbcZonesResponse(
        spot=payload.spot,
        today_low=payload.today_low,
        today_high=payload.today_high,
        bucket_pts=payload.bucket_pts,
        targets_above=[
            CbbcZoneCluster(
                bucket_low=z.bucket_low,
                bucket_high=z.bucket_high,
                direction=z.direction,
                distance_pts=z.distance_pts,
                notional_hkd=z.notional_hkd,
                contract_count=z.contract_count,
                outstanding_shares=z.outstanding_shares,
                nearest_call_level=z.nearest_call_level,
                safety_margin_pts=z.safety_margin_pts,
            )
            for z in payload.targets_above
        ],
        supports_below=[
            CbbcZoneCluster(
                bucket_low=z.bucket_low,
                bucket_high=z.bucket_high,
                direction=z.direction,
                distance_pts=z.distance_pts,
                notional_hkd=z.notional_hkd,
                contract_count=z.contract_count,
                outstanding_shares=z.outstanding_shares,
                nearest_call_level=z.nearest_call_level,
                safety_margin_pts=z.safety_margin_pts,
            )
            for z in payload.supports_below
        ],
        live_record_count=payload.live_record_count,
        killed_record_count=payload.killed_record_count,
        bull_setup=_setup_to_model(payload.bull_setup),
        bear_setup=_setup_to_model(payload.bear_setup),
    )


@app.get("/api/cbbc/zones", response_model=CbbcZonesResponse)
async def get_cbbc_zones():
    """返回当前 HSI 现价上方 / 下方 25pt 桶聚合的街货密集区。

    纯展示用途,不影响任何交易逻辑。已自动过滤:
    - ``street_vol == 0`` 的占位合约
    - 今天 HSI 触及过收回价的"已被吃掉"合约
    """
    return _build_cbbc_zones_response()


async def broadcast_cbbc_zones() -> None:
    """把 zones 数据推给所有 WS 客户端 (与 magnet_overlay 配套)。"""
    try:
        payload = _build_cbbc_zones_response()
    except Exception:  # noqa: BLE001
        return
    await manager.broadcast({"type": "cbbc_zones", "data": payload.model_dump()})


# --------------------------------------------------------------------------- #
# CBBC AI 决策顾问 (read-only, 完全独立于交易决策)
# --------------------------------------------------------------------------- #


@app.post("/api/cbbc/ai-advice", response_model=CbbcAiAdviceResponse)
async def request_cbbc_ai_advice():
    """触发一次性 AI 决策建议请求。

    流程:
    1. 收集当前 zones / state / market_regime 数据快照。
    2. 调用 ``cbbc_ai_advisor.request_advice`` 请求 OpenAI 兼容端点。
    3. 把结构化建议返给前端;失败时 ``ok=False`` 不抛异常。

    用途纯展示,不会触发任何下单动作。请求频率由前端按钮节流;
    后端不做缓存/不做 WS 推送(避免每次磁吸快照刷新都打 LLM)。
    """
    from cbbc_ai_advisor import request_advice as _request_advice

    if not bool(engine.cbbc_ai_advisor_enabled):
        return CbbcAiAdviceResponse(
            ok=False,
            error="AI 顾问未启用 (cbbc_ai_advisor_enabled=False)",
            model=str(engine.cbbc_ai_advisor_model or ""),
        )

    api_key = str(engine.cbbc_ai_advisor_api_key or "").strip()
    base_url = str(engine.cbbc_ai_advisor_base_url or "").strip()
    model_name = str(engine.cbbc_ai_advisor_model or "").strip()
    if not api_key:
        return CbbcAiAdviceResponse(
            ok=False, error="未配置 AI api_key", model=model_name or None,
        )
    if not base_url:
        return CbbcAiAdviceResponse(
            ok=False, error="未配置 AI base_url", model=model_name or None,
        )

    # Build snapshot dictionaries — same shapes the advisor module expects.
    try:
        zones_payload = _build_cbbc_zones_response().model_dump()
    except Exception as exc:  # noqa: BLE001
        return CbbcAiAdviceResponse(
            ok=False, error=f"无法构造 zones 快照: {exc}", model=model_name or None,
        )

    try:
        loop = asyncio.get_running_loop()
        state_obj = await loop.run_in_executor(None, engine.get_state)
        state_payload = state_obj.model_dump()
    except Exception as exc:  # noqa: BLE001
        return CbbcAiAdviceResponse(
            ok=False, error=f"无法读取 strategy state: {exc}", model=model_name or None,
        )

    regime_payload = None
    try:
        if engine.market_regime is not None:
            regime_payload = engine.market_regime.model_dump()
    except Exception:  # noqa: BLE001
        regime_payload = None

    advice = await _request_advice(
        zones=zones_payload,
        state=state_payload,
        regime=regime_payload,
        api_key=api_key,
        base_url=base_url,
        model=model_name or "claude-opus-4-7",
        api_style=str(engine.cbbc_ai_advisor_api_style or "openai"),
    )

    return CbbcAiAdviceResponse(
        ok=advice.ok,
        direction=advice.direction if advice.direction in ("bull", "bear", "skip") else None,
        confidence=advice.confidence,
        entry_low=advice.entry_low,
        entry_high=advice.entry_high,
        take_profit_1=advice.take_profit_1,
        take_profit_2=advice.take_profit_2,
        stop_loss=advice.stop_loss,
        rationale=advice.rationale,
        error=advice.error,
        raw_model_text=advice.raw_model_text,
        model=advice.model,
        elapsed_seconds=advice.elapsed_seconds,
    )


@app.post("/api/cbbc/hksi-style-ai-advice", response_model=HksiStyleAiAdviceResponse)
async def request_hksi_style_ai_advice():
    """觸發一次 HKSI-style AI 分析。

    使用同一套 ``cbbc_ai_advisor_*`` 設定，但輸出交易卡審閱格式。
    只作展示，不參與現有策略或下單。
    """
    from cbbc_hksi_style_ai_advisor import request_hksi_style_advice as _request_hksi_style_advice

    generated_at = ""
    if not bool(engine.cbbc_ai_advisor_enabled):
        return HksiStyleAiAdviceResponse(
            ok=False,
            generated_at=generated_at,
            source="error",
            error="AI 顧問未啟用 (cbbc_ai_advisor_enabled=False)",
            model=str(engine.cbbc_ai_advisor_model or ""),
        )

    api_key = str(engine.cbbc_ai_advisor_api_key or "").strip()
    base_url = str(engine.cbbc_ai_advisor_base_url or "").strip()
    model_name = str(engine.cbbc_ai_advisor_model or "").strip()
    if not api_key:
        return HksiStyleAiAdviceResponse(
            ok=False,
            generated_at=generated_at,
            source="error",
            error="未配置 AI api_key",
            model=model_name or None,
        )
    if not base_url:
        return HksiStyleAiAdviceResponse(
            ok=False,
            generated_at=generated_at,
            source="error",
            error="未配置 AI base_url",
            model=model_name or None,
        )

    try:
        zones_payload = _build_cbbc_zones_response().model_dump()
    except Exception as exc:  # noqa: BLE001
        return HksiStyleAiAdviceResponse(
            ok=False,
            generated_at=generated_at,
            source="error",
            error=f"無法構造 zones 快照: {exc}",
            model=model_name or None,
        )

    try:
        loop = asyncio.get_running_loop()
        state_obj = await loop.run_in_executor(None, engine.get_state)
        state_payload = state_obj.model_dump()
    except Exception as exc:  # noqa: BLE001
        return HksiStyleAiAdviceResponse(
            ok=False,
            generated_at=generated_at,
            source="error",
            error=f"無法讀取 strategy state: {exc}",
            model=model_name or None,
        )

    regime_payload = None
    try:
        if engine.market_regime is not None:
            regime_payload = engine.market_regime.model_dump()
    except Exception:  # noqa: BLE001
        regime_payload = None

    klines_payload = [item.model_dump() for item in engine.kline_history_1m[-120:]]
    result = await _request_hksi_style_advice(
        zones=zones_payload,
        state=state_payload,
        regime=regime_payload,
        klines=klines_payload,
        api_key=api_key,
        base_url=base_url,
        model=model_name or "claude-opus-4-7",
        api_style=str(engine.cbbc_ai_advisor_api_style or "openai"),
    )
    context = result.context or {}
    generated_at = str(context.get("generated_at", "") or "")
    return HksiStyleAiAdviceResponse(
        ok=result.ok,
        generated_at=generated_at,
        source=result.source,
        model=result.model,
        review=result.review,
        context=context,
        error=result.error,
        raw_model_text=result.raw_model_text,
        elapsed_seconds=result.elapsed_seconds,
    )


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
        if engine.market_regime is not None:
            await websocket.send_text(json.dumps({
                "type": "market_regime",
                "data": engine.market_regime.model_dump(),
            }, ensure_ascii=False, default=str))

        # cbbc-magnet-signal task 9.2: send the current magnet overlay so
        # the frontend can render call-level lines / dense band immediately
        # on connect (no waiting for the next state tick).
        try:
            payload = _build_magnet_overlay_payload()
            await websocket.send_text(json.dumps({
                "type": "magnet_overlay",
                "data": payload.model_dump(),
            }, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            pass

        # CBBC zones (read-only): same pattern, send on connect so the
        # frontend doesn't have to wait for the next state tick.
        try:
            zones = _build_cbbc_zones_response()
            await websocket.send_text(json.dumps({
                "type": "cbbc_zones",
                "data": zones.model_dump(),
            }, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            pass

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
