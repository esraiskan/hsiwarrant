"""
策略可调配置持久化。

包含 CBBC 街货磁吸信号模块（cbbc-magnet-signal）所需的 6 个运行时字段：
- ``cbbc_magnet_layer_enabled`` (bool, default ``False``)
- ``cbbc_intraday_polling_suspended`` (bool, default ``False``)
- ``cbbc_magnet_decay_points`` (float, default ``300.0``, 范围 ``(0.0, 10000.0]``)
- ``cbbc_dense_band_threshold_pts`` (float, default ``150.0``, 范围 ``[10.0, 1000.0]``)
- ``cbbc_dense_band_pull_share`` (float, default ``0.40``, 范围 ``[0.0, 1.0]``)
- ``cbbc_intraday_poll_interval_seconds`` (float, default ``60.0``, 范围 ``[10.0, 600.0]``)

加载时对上述字段做区间校验，遇到非有限浮点 / 类型错误 / 越界时回退默认值并写入
一条 ``level=WARN, source=runtime_config, event=config_corrupt_fallback_defaults`` 的
结构化日志（满足 cbbc-magnet-signal Requirement 9.3 / 9.6 / 9.9）。
"""
import json
import math
from pathlib import Path
from typing import Any


RUNTIME_CONFIG_PATH = Path(__file__).with_name("runtime_config.json")


# CBBC 磁吸模块运行时字段的内置默认值（cbbc-magnet-signal R9.1 / R9.3 / R9.4）。
# 暴露为公开常量，供 ``HSIStrategyEngine`` 在构造时初始化默认值（见后续任务 7.2）。
CBBC_FIELD_DEFAULTS: dict[str, Any] = {
    "cbbc_magnet_layer_enabled": False,
    "cbbc_intraday_polling_suspended": False,
    "cbbc_magnet_decay_points": 300.0,
    "cbbc_dense_band_threshold_pts": 150.0,
    "cbbc_dense_band_pull_share": 0.40,
    "cbbc_intraday_poll_interval_seconds": 60.0,
    # CBBC 磁吸方向闸门 (UX 增强 — 不在原 spec 范围,默认关闭以保持回归安全)。
    # 启用后:|bias| >= cbbc_magnet_direction_gate_threshold 时阻止逆向入场。
    #   bias > 0 (向下吸) → 阻止 BULL 入场,只准 BEAR
    #   bias < 0 (向上吸) → 阻止 BEAR 入场,只准 BULL
    # layer disabled / degraded / 不可用时闸门不生效 (fail-safe)。
    "cbbc_magnet_direction_gate_enabled": False,
    "cbbc_magnet_direction_gate_threshold": 0.15,
    # CBBC AI 决策顾问 (read-only, UX 增强 — 默认关闭, 完全不影响交易决策)。
    # 启用后, 前端可通过 POST /api/cbbc/ai-advice 触发一次性请求,
    # 由 ``cbbc_ai_advisor.request_advice`` 调用 OpenAI 兼容端点。
    # api_key 为空时端点返回 ok=False / error="未配置 AI api_key"。
    "cbbc_ai_advisor_enabled": False,
    "cbbc_ai_advisor_base_url": "http://127.0.0.1:8765",
    "cbbc_ai_advisor_model": "claude-opus-4-7",
    "cbbc_ai_advisor_api_key": "",
    # API 协议风格:"openai" 用 /v1/chat/completions + Authorization Bearer;
    # "anthropic" 用 /v1/messages + x-api-key + anthropic-version。
    "cbbc_ai_advisor_api_style": "openai",
}

# 浮点字段的有效区间：(low, high, low_inclusive, high_inclusive)。
_CBBC_FLOAT_RANGES: dict[str, tuple[float, float, bool, bool]] = {
    "cbbc_magnet_decay_points": (0.0, 10000.0, False, True),       # (0.0, 10000.0]
    "cbbc_dense_band_threshold_pts": (10.0, 1000.0, True, True),    # [10.0, 1000.0]
    "cbbc_dense_band_pull_share": (0.0, 1.0, True, True),           # [0.0, 1.0]
    "cbbc_intraday_poll_interval_seconds": (10.0, 600.0, True, True),  # [10.0, 600.0]
    "cbbc_magnet_direction_gate_threshold": (0.0, 1.0, True, True),    # [0.0, 1.0]
}

_CBBC_BOOL_FIELDS: tuple[str, ...] = (
    "cbbc_magnet_layer_enabled",
    "cbbc_intraday_polling_suspended",
    "cbbc_magnet_direction_gate_enabled",
    "cbbc_ai_advisor_enabled",
)

# 字符串字段:类型不匹配时回退到默认值,但不做内容校验 (URL/key/model name)。
_CBBC_STRING_FIELDS: tuple[str, ...] = (
    "cbbc_ai_advisor_base_url",
    "cbbc_ai_advisor_model",
    "cbbc_ai_advisor_api_key",
    "cbbc_ai_advisor_api_style",
)


def _emit_corrupt_warn(*, field: str | None, value: Any, reason: str) -> None:
    """写入一条 cbbc-magnet-signal R9.9 要求的结构化 WARN 日志。"""
    field_part = f" field={field}" if field is not None else ""
    print(
        "[RuntimeConfig] level=WARN source=runtime_config "
        "event=config_corrupt_fallback_defaults"
        f"{field_part} value={value!r} reason={reason}"
    )


def _validate_cbbc_bool(name: str, raw: Any) -> tuple[bool, bool, str]:
    if isinstance(raw, bool):
        return True, raw, ""
    return False, bool(CBBC_FIELD_DEFAULTS[name]), "type_mismatch"


def _validate_cbbc_float(name: str, raw: Any) -> tuple[bool, float, str]:
    low, high, low_inc, high_inc = _CBBC_FLOAT_RANGES[name]
    # 拒绝 ``bool``：``isinstance(True, int)`` 为真，但布尔不应被当作浮点配置值。
    if isinstance(raw, bool):
        return False, float(CBBC_FIELD_DEFAULTS[name]), "type_mismatch"
    if not isinstance(raw, (int, float)):
        return False, float(CBBC_FIELD_DEFAULTS[name]), "type_mismatch"
    value = float(raw)
    if not math.isfinite(value):
        return False, float(CBBC_FIELD_DEFAULTS[name]), "non_finite"
    low_ok = value >= low if low_inc else value > low
    high_ok = value <= high if high_inc else value < high
    if not (low_ok and high_ok):
        return False, float(CBBC_FIELD_DEFAULTS[name]), "out_of_range"
    return True, value, ""


def _sanitize_cbbc_fields(data: dict[str, Any]) -> dict[str, Any]:
    """按 cbbc-magnet-signal R9.3 / R9.6 校验 6 个 CBBC 字段。

    - 字段缺失：保持缺失，由调用方在构造时填入默认值，避免破坏既有保存格式。
    - 字段存在但非法（类型错误 / 非有限 / 越界）：替换为默认值，并写一条
      ``event=config_corrupt_fallback_defaults`` 的 WARN 日志。
    """
    for name in _CBBC_BOOL_FIELDS:
        if name not in data:
            continue
        raw = data[name]
        ok, value, reason = _validate_cbbc_bool(name, raw)
        if not ok:
            _emit_corrupt_warn(field=name, value=raw, reason=reason)
        data[name] = value
    for name in _CBBC_FLOAT_RANGES:
        if name not in data:
            continue
        raw = data[name]
        ok, value, reason = _validate_cbbc_float(name, raw)
        if not ok:
            _emit_corrupt_warn(field=name, value=raw, reason=reason)
        data[name] = value
    for name in _CBBC_STRING_FIELDS:
        if name not in data:
            continue
        raw = data[name]
        if not isinstance(raw, str):
            _emit_corrupt_warn(field=name, value=raw, reason="type_mismatch")
            data[name] = str(CBBC_FIELD_DEFAULTS[name])
    return data


def load_runtime_config(path: Path = RUNTIME_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _emit_corrupt_warn(field=None, value=str(path), reason=f"json_parse_failed:{e}")
        return {}
    if not isinstance(data, dict):
        _emit_corrupt_warn(field=None, value=type(data).__name__, reason="non_object_root")
        return {}
    return _sanitize_cbbc_fields(data)


def save_runtime_config(config: dict[str, Any], path: Path = RUNTIME_CONFIG_PATH) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        tmp_path.replace(path)
        return True
    except Exception as e:
        print(f"[RuntimeConfig] 写入配置失败: {e}")
        return False
