"""
CBBC AI 决策顾问 (read-only,完全独立于交易决策路径)。

接入一个 OpenAI 兼容的 /v1/chat/completions 端点 (你本机的代理或自托管模型),
把当前的 CBBC zones / magnet state / market regime 打包成 prompt,要求模型
返回 *结构化* JSON: 推荐方向 / 入场区 / 止盈 / 止损 / 信心 / 理由。

设计要点:
- ``request_advice(...)`` 是异步纯函数,失败时返回带 ``error`` 字段的载荷,
  从不抛异常上行到 FastAPI 路由 — 让前端能稳定显示错误状态。
- HTTP 调用走 ``httpx.AsyncClient``;用本地 endpoint 时不强制 HTTPS。
- 模型输出必须能解析成 ``AiAdviceResponse``;解析失败时退化为 ``error`` 状态。
- 不依赖 ``CbbcDataService`` 等已有 CBBC 模块,避免耦合。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("cbbc_ai_advisor")


# 默认值 — 可被 runtime_config 覆写。
DEFAULT_AI_BASE_URL: str = "http://127.0.0.1:8765"
DEFAULT_AI_MODEL: str = "claude-opus-4-7"  # 用户本机代理实测可用
DEFAULT_AI_TIMEOUT_SECONDS: float = 30.0
DEFAULT_MAX_TOKENS: int = 2500
# 协议风格:
# - "openai" → /v1/chat/completions, "Authorization: Bearer ..."
# - "anthropic" → /v1/messages, "x-api-key: ..." + "anthropic-version: 2023-06-01"
DEFAULT_API_STYLE: str = "openai"


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AiAdviceResponse:
    """AI 顾问返回的结构化建议。

    ``ok=True`` 时所有交易字段必须有效;``ok=False`` 时只看 ``error``。
    """
    ok: bool
    direction: Optional[str] = None  # "bull" | "bear" | "skip" | None
    confidence: Optional[float] = None  # 0..1
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    stop_loss: Optional[float] = None
    rationale: Optional[str] = None
    error: Optional[str] = None  # 仅当 ok=False 时填充
    raw_model_text: Optional[str] = None  # 调试用 — 模型原始输出
    model: Optional[str] = None
    elapsed_seconds: Optional[float] = None


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


_SYSTEM_PROMPT = """\
你是恒指 (HSI) 牛熊证 (CBBC) 街货分布的短线交易顾问。
基于用户提供的实时数据,给出**单一明确**的交易建议。

输出**必须**是合法 JSON 对象,字段如下,不允许任何多余文字:
{"direction":"bull"|"bear"|"skip","confidence":<0..1>,"entry_low":<f>,"entry_high":<f>,"take_profit_1":<f>,"take_profit_2":<f>,"stop_loss":<f>,"rationale":"<不超过 60 字中文>"}

规则:
- direction="skip" 时其它价位字段填 null。
- bull: entry < tp1 < tp2, sl < entry。bear: entry > tp1 > tp2, sl > entry。
- R:R ≥ 1.0 否则 "skip";入场区宽 ≤ 30 点;止损不能扛回 100 点以上。
- 直接返回纯 JSON 文本,不要 markdown 代码块。
"""


def _format_zones_for_prompt(zones: Mapping[str, Any]) -> str:
    """把 ``CbbcZonesResponse`` 字典格式化成易读文本块。"""
    lines: list[str] = []
    spot = zones.get("spot")
    today_low = zones.get("today_low")
    today_high = zones.get("today_high")
    bucket_pts = zones.get("bucket_pts", 25)
    live = zones.get("live_record_count", 0)
    killed = zones.get("killed_record_count", 0)

    lines.append(f"HSI 现价: {spot}")
    lines.append(f"今日 low: {today_low}, high: {today_high}")
    lines.append(f"街货桶宽: {bucket_pts}pt;活跃合约 {live};已被吃 {killed}")

    targets = zones.get("targets_above", []) or []
    supports = zones.get("supports_below", []) or []

    if targets:
        lines.append("\n上方街货群 (做多目标 / 做空止损):")
        lines.append("  序号  价位     距离      合约数  街货拉力 (HKD)")
        for i, t in enumerate(targets[:6], start=1):
            lines.append(
                f"  #{i}    {t['bucket_low']:.0f}    "
                f"{t['distance_pts']:+.0f}pt    "
                f"{t['contract_count']:>3d}     "
                f"{t['notional_hkd']:>14,.0f}"
            )
    else:
        lines.append("\n上方无显著街货群。")

    if supports:
        lines.append("\n下方街货群 (做空目标 / 做多止损):")
        lines.append("  序号  价位     距离      合约数  街货拉力 (HKD)")
        for i, s in enumerate(supports[:6], start=1):
            lines.append(
                f"  #{i}    {s['bucket_low']:.0f}    "
                f"{s['distance_pts']:+.0f}pt    "
                f"{s['contract_count']:>3d}     "
                f"{s['notional_hkd']:>14,.0f}"
            )
    else:
        lines.append("\n下方无显著街货群。")

    return "\n".join(lines)


def _format_magnet_state(state: Mapping[str, Any]) -> str:
    bias = state.get("cbbc_magnet_bias")
    nb = state.get("cbbc_nearest_bull_distance_pts")
    nbear = state.get("cbbc_nearest_bear_distance_pts")
    layer = state.get("cbbc_magnet_layer_enabled")
    degraded = state.get("cbbc_magnet_degraded")

    bias_str = f"{bias:+.2f}" if isinstance(bias, (int, float)) else "N/A"
    return (
        f"磁吸 bias = {bias_str} (>0 上方贴身;<0 下方贴身);"
        f"最近牛证距离 {nb}pt,最近熊证距离 {nbear}pt;"
        f"layer_enabled={layer}, degraded={degraded}"
    )


def _format_market_regime(regime: Optional[Mapping[str, Any]]) -> str:
    if not regime:
        return "MarketRegime: 不可用"
    return (
        f"MarketRegime: {regime.get('regime', '?')} ({regime.get('label', '?')}, bias={regime.get('bias', '?')}, "
        f"confidence={regime.get('confidence', '?')});"
        f"day_open={regime.get('day_open')}, current_price={regime.get('current_price')};"
        f"建议: {regime.get('advice', '')}"
    )


def _build_user_prompt(
    *,
    zones: Mapping[str, Any],
    state: Mapping[str, Any],
    regime: Optional[Mapping[str, Any]],
    extra_notes: Optional[str] = None,
) -> str:
    parts: list[str] = []
    parts.append("=== 实时数据快照 ===")
    parts.append(_format_zones_for_prompt(zones))
    parts.append("")
    parts.append("=== 磁吸状态 ===")
    parts.append(_format_magnet_state(state))
    parts.append("")
    parts.append("=== 市场体制 ===")
    parts.append(_format_market_regime(regime))
    if extra_notes:
        parts.append("")
        parts.append("=== 用户备注 ===")
        parts.append(extra_notes)
    parts.append("")
    parts.append(
        "请基于以上数据,给出**单一明确**的交易建议 (bull / bear / skip)。"
        "记住:输出必须是合法 JSON,无任何额外文字。"
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# JSON parsing
# --------------------------------------------------------------------------- #


def _extract_json_object(text: str) -> Optional[dict]:
    """从模型输出中提取第一个完整的 JSON 对象。

    模型有时会包 ```json ... ``` 围栏;有时会在前面加 "好的," 之类。
    用 ``re`` 找到第一个 ``{`` 然后用栈匹配到对应的 ``}``。
    """
    if not text:
        return None
    text = text.strip()
    # Strip ```json fences if present.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    obj = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                if isinstance(obj, dict):
                    return obj
                return None
    return None


def _parse_advice(
    raw_text: str,
    *,
    model: Optional[str],
    elapsed_seconds: float,
) -> AiAdviceResponse:
    """把模型输出解析成 ``AiAdviceResponse``;失败时返回 ``ok=False``。"""
    obj = _extract_json_object(raw_text)
    if obj is None:
        return AiAdviceResponse(
            ok=False,
            error="模型输出无法解析为 JSON",
            raw_model_text=raw_text,
            model=model,
            elapsed_seconds=elapsed_seconds,
        )

    direction = str(obj.get("direction", "")).lower()
    if direction not in ("bull", "bear", "skip"):
        return AiAdviceResponse(
            ok=False,
            error=f"direction 字段非法: {direction!r}",
            raw_model_text=raw_text,
            model=model,
            elapsed_seconds=elapsed_seconds,
        )

    def _f(name: str) -> Optional[float]:
        v = obj.get(name)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    confidence = _f("confidence")
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))

    return AiAdviceResponse(
        ok=True,
        direction=direction,
        confidence=confidence,
        entry_low=_f("entry_low"),
        entry_high=_f("entry_high"),
        take_profit_1=_f("take_profit_1"),
        take_profit_2=_f("take_profit_2"),
        stop_loss=_f("stop_loss"),
        rationale=str(obj.get("rationale", "")),
        raw_model_text=raw_text,
        model=model,
        elapsed_seconds=elapsed_seconds,
    )


# --------------------------------------------------------------------------- #
# HTTP call
# --------------------------------------------------------------------------- #


class _RateLimitError(RuntimeError):
    """代理返回 429 / 503 速率限制 — 单独区分以支持降级和重试逻辑。"""
    def __init__(self, status_code: int, body: str):
        super().__init__(
            f"AI endpoint rate-limited (HTTP {status_code}): {body[:300]}"
        )
        self.status_code = status_code
        self.body = body


async def _post_chat_completion_once(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: float,
    max_tokens: int,
    api_style: str = DEFAULT_API_STYLE,
) -> tuple[str, float]:
    """调用 LLM 端点,返回 ``(content, elapsed_seconds)``。

    支持两种协议:
    - ``api_style="openai"`` → ``/v1/chat/completions`` + ``Authorization: Bearer``
    - ``api_style="anthropic"`` → ``/v1/messages`` + ``x-api-key`` + ``anthropic-version``

    使用 SSE 流式响应:经验上一些代理 (本机的 OpenAI 兼容代理) 在非流模式
    下会硬截断 content 到 ~150 字符,但流式 (``stream=true``) 能拿到完整输出。

    速率限制 (429 / 503) 单独抛 ``_RateLimitError``,让上层决定重试还是降级。
    """
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"httpx not available: {exc!r}") from exc

    base = base_url.rstrip("/")
    style = (api_style or DEFAULT_API_STYLE).lower()

    if style == "anthropic":
        # 兼容用户给 "/v1" 前缀的或仅给根。
        if base.endswith("/v1"):
            url = f"{base}/messages"
        else:
            url = f"{base}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": 0.3,
            "stream": True,
        }
    else:  # openai
        if base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "stream": True,
        }

    chunks: list[str] = []
    start = asyncio.get_event_loop().time()
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code in (429, 503):
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise _RateLimitError(resp.status_code, body)
            if resp.status_code == 401:
                raise RuntimeError(
                    f"AI 端点拒绝鉴权 (HTTP 401);api_key 对模型 {model!r} 无访问权限"
                )
            if resp.status_code >= 400:
                body = await resp.aread()
                text_body = body.decode("utf-8", errors="replace")[:500]
                raise RuntimeError(
                    f"AI endpoint returned HTTP {resp.status_code}: {text_body}"
                )
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                # OpenAI: choices[0].delta.content
                # Anthropic: content_block_delta event with delta.text
                delta_text: Optional[str] = None
                evt_type = evt.get("type")
                if evt_type == "content_block_delta":
                    d = evt.get("delta", {})
                    if isinstance(d, dict):
                        delta_text = d.get("text")
                else:
                    try:
                        delta_text = evt["choices"][0].get("delta", {}).get("content")
                    except (KeyError, IndexError, TypeError):
                        delta_text = None
                if isinstance(delta_text, str):
                    chunks.append(delta_text)
        elapsed = asyncio.get_event_loop().time() - start

    content = "".join(chunks)
    if not content:
        raise RuntimeError(
            "Empty model content (streamed); 可能是模型超时或代理未输出任何 token。"
        )
    return content, elapsed


# 模型降级链:opus → 3.7-sonnet → ... 当代理对当前模型限流时,自动尝试同 api_key 仍授权的兄弟模型。
# 用户的本地代理实测仅 ``claude-opus-4-7`` 与
# ``claude-3-7-sonnet-20250219`` 可访问,其它 4-x 系列返回 401。
_MODEL_FALLBACK_CHAIN: dict[str, list[str]] = {
    "claude-opus-4-7": ["claude-3-7-sonnet-20250219"],
    "claude-opus-4-7-thinking": ["claude-opus-4-7", "claude-3-7-sonnet-20250219"],
    "claude-sonnet-4-7": ["claude-3-7-sonnet-20250219"],
    "claude-sonnet-4-7-thinking": ["claude-3-7-sonnet-20250219"],
    "claude-haiku-4-7": ["claude-3-7-sonnet-20250219"],
    "claude-opus-4-6": ["claude-opus-4-7", "claude-3-7-sonnet-20250219"],
    "claude-sonnet-4-6": ["claude-opus-4-7", "claude-3-7-sonnet-20250219"],
}


async def _post_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: float,
    max_tokens: int,
    api_style: str = DEFAULT_API_STYLE,
) -> tuple[str, float, str]:
    """带速率限制重试 + 模型降级的封装。

    返回 ``(content, elapsed_seconds, used_model)``。

    流程:
    1. 用首选 ``model`` 试一次。
    2. 拿到 429/503 → 等 0.8s 重试一次 (代理可能在切账号)。
    3. 仍是 429/503 → 顺着 ``_MODEL_FALLBACK_CHAIN`` 一步步降级,直到成功或耗尽。
    4. 全部用尽 → 抛最后一个 ``_RateLimitError``,在 ``request_advice`` 里被翻译成中文消息。
    """
    candidates = [model] + _MODEL_FALLBACK_CHAIN.get(model, [])
    last_exc: Exception | None = None
    total_start = asyncio.get_event_loop().time()
    # 同模型最多 3 次,带退避: 0.8s -> 2.0s -> 4.0s。
    # 速率限制通常 1~5s 内会恢复 (代理切到下一个账号),给点耐心。
    _per_model_backoffs = (0.8, 2.0, 4.0)
    for attempt, candidate in enumerate(candidates):
        for retry, backoff in enumerate(_per_model_backoffs):
            try:
                content, _ = await _post_chat_completion_once(
                    base_url=base_url,
                    api_key=api_key,
                    model=candidate,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    timeout_seconds=timeout_seconds,
                    max_tokens=max_tokens,
                    api_style=api_style,
                )
                elapsed = asyncio.get_event_loop().time() - total_start
                if candidate != model:
                    logger.warning(
                        "AI advisor 因速率限制从 %s 降级到 %s",
                        model, candidate,
                    )
                return content, elapsed, candidate
            except _RateLimitError as exc:
                last_exc = exc
                if retry + 1 < len(_per_model_backoffs):
                    await asyncio.sleep(backoff)
                    continue
                # 同模型已用尽重试 → 跳到下一档。
                break
            except Exception:
                # 非速率限制的错误直接上抛,不要穿模型降级。
                raise
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def request_advice(
    *,
    zones: Mapping[str, Any],
    state: Mapping[str, Any],
    regime: Optional[Mapping[str, Any]] = None,
    extra_notes: Optional[str] = None,
    api_key: str,
    base_url: str = DEFAULT_AI_BASE_URL,
    model: str = DEFAULT_AI_MODEL,
    timeout_seconds: float = DEFAULT_AI_TIMEOUT_SECONDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    api_style: str = DEFAULT_API_STYLE,
) -> AiAdviceResponse:
    """异步调用 AI endpoint,返回结构化建议。

    任何异常都会被包装成 ``ok=False`` 的响应,不会上抛。
    """
    if not api_key:
        return AiAdviceResponse(ok=False, error="未配置 AI api_key", model=model)
    if not base_url:
        return AiAdviceResponse(ok=False, error="未配置 AI base_url", model=model)

    user_prompt = _build_user_prompt(
        zones=zones, state=state, regime=regime, extra_notes=extra_notes
    )

    try:
        raw_text, elapsed, used_model = await _post_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            api_style=api_style,
        )
    except _RateLimitError as exc:
        logger.warning("AI endpoint rate-limited even after fallback: %r", exc)
        return AiAdviceResponse(
            ok=False,
            error=(
                f"AI 端点达速率限制 (HTTP {exc.status_code});"
                f"已尝试主模型 {model} 及降级模型,均被限流。请稍后再试或换 api_key。"
            ),
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI endpoint call failed: %r", exc)
        return AiAdviceResponse(
            ok=False,
            error=f"AI 端点调用失败: {type(exc).__name__}: {exc}",
            model=model,
        )

    return _parse_advice(raw_text, model=used_model, elapsed_seconds=elapsed)


__all__ = [
    "AiAdviceResponse",
    "DEFAULT_AI_BASE_URL",
    "DEFAULT_AI_MODEL",
    "DEFAULT_AI_TIMEOUT_SECONDS",
    "DEFAULT_MAX_TOKENS",
    "request_advice",
]
