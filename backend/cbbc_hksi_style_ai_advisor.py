"""
HKSI-style CBBC AI analyst (read-only).

This module reuses the existing AI endpoint settings, but asks the model for a
HKSI trade-card style review: main direction, status, entry choices, stop loss,
take profit, give-up conditions, product warning, and machine-readable levels.
It does not feed into the live strategy or order path.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from cbbc_ai_advisor import _post_chat_completion


ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
ALLOWED_ACTIONABILITY = {"observe", "wait_for_confirmation", "reduce_size", "avoid_trade"}
ALLOWED_STATUS = {"空倉等待", "持倉觀察", "等待確認", "不交易"}
ALLOWED_ACTIONS = {"買升", "買跌", "不做"}
ALLOWED_SIDES = {"bull", "bear", "none"}
ALLOWED_EXECUTION_ACTIONS = {"buy_bull", "buy_bear", "none"}
ALLOWED_VWAP_RELATIONS = {"any", "above", "below"}
ALLOWED_COEFFICIENT_DIRECTIONS = {"UP", "DOWN", "NEUTRAL"}


@dataclass(frozen=True)
class HksiStyleAdviceResult:
    ok: bool
    review: Optional[dict[str, Any]] = None
    context: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    raw_model_text: Optional[str] = None
    model: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    source: str = "openai"


_SYSTEM_PROMPT = """\
你係 HKSI 風格嘅恆指牛熊證 AI 風險審閱員。
你會收到本系統即市資料，包括 HSI K 線、RSI、VWAP、market regime、CBBC 街貨密集區、磁吸狀態同現有持倉。

硬性規則：
1. 只可以根據輸入 JSON 分析，不可以引用外部資料。
2. 必須使用繁體中文香港交易語境。
3. 不可聲稱知道未來走勢。
4. confidence 只代表當前資料支持度，不係勝率。
5. 若 VWAP、價位、RSI、市況或街貨訊號矛盾，必須用「等待確認」或「不交易」。
6. 不可叫用戶重倉、瞓身或必買；金額要保守。
7. 此輸出只係 read-only 分析，不會觸發下單。

只可以有四種 trade_plan.status：
- 空倉等待：方向清晰，可以畀系統/人工觀察入場條件。
- 持倉觀察：已有倉時，重點係止蝕、食糊及放棄條件。
- 等待確認：方向有傾向但未夠條件，不可正式入場。
- 不交易：資料不足、矛盾、風險過高或不宜交易。

必須輸出合法 JSON object，不要 markdown，不要加 schema 以外文字。
JSON 必須有以下欄位：
{
  "summary": "一句總結",
  "trade_plan": {
    "main_direction": "偏升 [up] | 中性偏升 [up] | 中性 [neutral] | 中性偏跌 [down] | 偏跌 [down]",
    "status": "空倉等待 | 持倉觀察 | 等待確認 | 不交易",
    "entry_plan_1": {"condition": "...", "action": "買升|買跌|不做", "amount": 0},
    "entry_plan_2": {"condition": "...", "action": "買升|買跌|不做", "amount": 0},
    "stop_loss": ["..."],
    "take_profit": ["..."],
    "give_up_conditions": ["..."],
    "product_warning": "...",
    "summary": "..."
  },
  "execution_plan": {
    "enabled": false,
    "side": "bull|bear|none",
    "entry_rules": [
      {
        "label": "...",
        "action": "buy_bull|buy_bear|none",
        "price_min": 0,
        "price_max": 0,
        "rsi_min": 0,
        "rsi_max": 100,
        "vwap_relation": "any|above|below",
        "amount": 0,
        "priority": 1,
        "comment": "..."
      }
    ],
    "take_profit_levels": [],
    "stop_loss_levels": [],
    "give_up_levels": [],
    "max_position_hkd": 0,
    "time_in_force": "intraday",
    "notes": "..."
  },
  "trade_coefficients": {
    "direction": "UP|DOWN|NEUTRAL",
    "entry_min": 0,
    "entry_max": 0,
    "share_count": 0,
    "take_profit_min": 0,
    "take_profit_max": 0,
    "stop_loss_min": 0,
    "stop_loss_max": 0,
    "confidence": 0,
    "notes": "..."
  },
  "risk_level": "low|medium|high",
  "confidence_comment": "...",
  "key_supporting_points": ["..."],
  "conflicts": ["..."],
  "watch_levels": ["..."],
  "data_quality_notes": ["..."],
  "suggested_user_action": "...",
  "actionability": "observe|wait_for_confirmation|reduce_size|avoid_trade",
  "limitations": ["..."]
}

價格位要求：
- entry / take_profit / stop_loss 都係 HSI 點位。
- 如果不建議交易，status 用「不交易」或「等待確認」，entry amount 必須 0，execution_plan.enabled=false，trade_coefficients.direction="NEUTRAL"。
- 若正式可觀察入場，仍要保守；一般 amount 3000-5000 HKD。
- 做牛通常要現價企穩 VWAP 或 RSI 回升確認；做熊通常要低於 VWAP 或反彈失敗。
- give_up_conditions 必須至少包含 VWAP、街貨密集區/磁吸、或 market regime 轉向其中一項。
- product_warning 必須提醒牛熊證收回價距離，不可揀太貼價產品。
"""


def build_context(
    *,
    zones: Mapping[str, Any],
    state: Mapping[str, Any],
    regime: Optional[Mapping[str, Any]],
    klines: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the context dict shipped to the model.

    Designed to be **compact** (≤ 2K tokens) to avoid blowing the proxy's
    rate-limit budget. Instead of dumping 80 full klines, we send a 5-bar
    snapshot + summary statistics. The model can still reason about trend,
    range, RSI/VWAP relationship through the ``derived`` block which already
    contains pre-computed signals.
    """
    latest = klines[-1] if klines else {}
    recent = klines[-30:] if klines else []  # 30 bars for context reasoning
    last_5 = recent[-5:] if recent else []
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": "HSI",
        "read_only": True,
        "latest": _compact_kline(latest),
        # Send only the last 5 bars as full structured rows. The other 25
        # are summarized below — the model has enough to reason about
        # micro-trend without paying for 80 × 9-field json bloat.
        "last_5_klines": [_compact_kline(row) for row in last_5],
        "klines_summary": _summarize_klines(recent),
        "strategy_state": _compact_state(state),
        "market_regime": _compact_regime(regime),
        "cbbc_zones": _compact_zones(zones),
        "derived": _derive_context(zones=zones, state=state, regime=regime, klines=recent),
    }


def _compact_kline(row: Mapping[str, Any]) -> dict[str, Any]:
    """Trim a kline row to fields the model actually needs."""
    if not isinstance(row, Mapping):
        return {}
    return {
        "time": row.get("time"),
        "open": _safe_float(row.get("open")),
        "high": _safe_float(row.get("high")),
        "low": _safe_float(row.get("low")),
        "close": _safe_float(row.get("close")),
        "volume": _safe_float(row.get("volume")),
        "rsi": round(_safe_float(row.get("rsi")), 2) if row.get("rsi") else None,
        "vwap": round(_safe_float(row.get("vwap")), 2) if row.get("vwap") else None,
    }


def _summarize_klines(klines: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Compress a window of klines into a few aggregate signals."""
    if not klines:
        return {"count": 0}
    closes = [_safe_float(r.get("close")) for r in klines if _safe_float(r.get("close")) > 0]
    highs = [_safe_float(r.get("high")) for r in klines if _safe_float(r.get("high")) > 0]
    lows = [_safe_float(r.get("low")) for r in klines if _safe_float(r.get("low")) > 0]
    rsis = [_safe_float(r.get("rsi")) for r in klines if _safe_float(r.get("rsi")) > 0]
    vwaps = [_safe_float(r.get("vwap")) for r in klines if _safe_float(r.get("vwap")) > 0]
    vols = [_safe_float(r.get("volume")) for r in klines if _safe_float(r.get("volume")) > 0]
    if not closes:
        return {"count": len(klines)}
    first = closes[0]
    last = closes[-1]
    diff = last - first
    if abs(diff) < 5:
        trend = "flat"
    elif diff > 0:
        trend = "rising"
    else:
        trend = "falling"
    bars_above_vwap = (
        sum(1 for c, v in zip(closes, vwaps) if c > v)
        if len(vwaps) == len(closes) else None
    )
    return {
        "count": len(closes),
        "first_close": round(first, 2),
        "last_close": round(last, 2),
        "diff": round(diff, 2),
        "high": round(max(highs), 2) if highs else None,
        "low": round(min(lows), 2) if lows else None,
        "range": round(max(highs) - min(lows), 2) if highs and lows else None,
        "trend": trend,
        "rsi_now": round(rsis[-1], 2) if rsis else None,
        "rsi_min": round(min(rsis), 2) if rsis else None,
        "rsi_max": round(max(rsis), 2) if rsis else None,
        "vwap_now": round(vwaps[-1], 2) if vwaps else None,
        "bars_above_vwap": bars_above_vwap,
        "avg_volume": round(sum(vols) / len(vols), 0) if vols else None,
        "last_volume": round(vols[-1], 0) if vols else None,
    }


def _compact_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Strategy state — keep only the trade-relevant fields."""
    if not isinstance(state, Mapping):
        return {}
    keep = (
        "position", "entry_price", "current_price",
        "unrealized_pnl", "unrealized_pnl_hkd", "total_pnl_hkd",
        "trade_count", "win_count", "loss_count", "is_running",
        "cbbc_magnet_layer_enabled", "cbbc_magnet_degraded",
        "cbbc_magnet_bias",
        "cbbc_nearest_bull_distance_pts", "cbbc_nearest_bear_distance_pts",
        "breadth_ratio", "breadth_amplitude",
    )
    return {k: state.get(k) for k in keep if k in state}


def _compact_regime(regime: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    if not isinstance(regime, Mapping):
        return None
    keep = (
        "regime", "label", "bias", "confidence", "advice",
        "current_price", "day_open", "previous_close",
        "opening_range_high", "opening_range_low", "day_position_pct",
    )
    return {k: regime.get(k) for k in keep if k in regime}


def _compact_zones(zones: Mapping[str, Any]) -> dict[str, Any]:
    """Trim zones response to top-3 targets/supports + spot/extremes only."""
    if not isinstance(zones, Mapping):
        return {}
    targets = zones.get("targets_above") or []
    supports = zones.get("supports_below") or []
    return {
        "spot": zones.get("spot"),
        "today_low": zones.get("today_low"),
        "today_high": zones.get("today_high"),
        "bucket_pts": zones.get("bucket_pts"),
        "live_record_count": zones.get("live_record_count"),
        "killed_record_count": zones.get("killed_record_count"),
        # Keep only top-3 of each side; that's all the model can reason
        # about anyway. Use nearest_call_level (real strike) over bucket_low
        # so the model doesn't get confused by bucket-rounding artifacts.
        "targets_above": [_compact_zone(z) for z in targets[:3]],
        "supports_below": [_compact_zone(z) for z in supports[:3]],
        "bull_setup": zones.get("bull_setup"),
        "bear_setup": zones.get("bear_setup"),
    }


def _compact_zone(z: Any) -> dict[str, Any]:
    if not isinstance(z, Mapping):
        return {}
    return {
        "real_call_level": z.get("nearest_call_level") or z.get("bucket_low"),
        "bucket_low": z.get("bucket_low"),
        "direction": z.get("direction"),
        "distance_pts": z.get("distance_pts"),
        "notional_hkd": z.get("notional_hkd"),
        "contract_count": z.get("contract_count"),
        "safety_margin_pts": z.get("safety_margin_pts"),
    }


def _derive_context(
    *,
    zones: Mapping[str, Any],
    state: Mapping[str, Any],
    regime: Optional[Mapping[str, Any]],
    klines: list[Mapping[str, Any]],
) -> dict[str, Any]:
    closes = [_safe_float(row.get("close")) for row in klines if _safe_float(row.get("close")) > 0]
    latest = klines[-1] if klines else {}
    price = _safe_float(latest.get("close")) or _safe_float(state.get("current_price")) or _safe_float(zones.get("spot"))
    vwap = _safe_float(latest.get("vwap"))
    rsi = _safe_float(latest.get("rsi"))
    today_low = _safe_float(zones.get("today_low"))
    today_high = _safe_float(zones.get("today_high"))
    if closes:
        today_low = today_low or min(closes)
        today_high = today_high or max(closes)
    atr_proxy = max(40.0, (today_high - today_low) * 0.25) if today_high > today_low else 80.0
    targets_above = zones.get("targets_above") if isinstance(zones.get("targets_above"), list) else []
    supports_below = zones.get("supports_below") if isinstance(zones.get("supports_below"), list) else []
    nearest_target = targets_above[0] if targets_above else {}
    nearest_support = supports_below[0] if supports_below else {}
    return {
        "price": round(price, 2),
        "vwap": round(vwap, 2),
        "price_vs_vwap": round(price - vwap, 2) if price > 0 and vwap > 0 else None,
        "rsi": round(rsi, 2) if rsi > 0 else None,
        "today_low": round(today_low, 2) if today_low > 0 else None,
        "today_high": round(today_high, 2) if today_high > 0 else None,
        "atr_proxy": round(atr_proxy, 2),
        "nearest_upper_bear_zone": nearest_target,
        "nearest_lower_bull_zone": nearest_support,
        "regime_bias": regime.get("bias") if isinstance(regime, Mapping) else None,
        "magnet_bias": state.get("cbbc_magnet_bias"),
        "nearest_bull_distance_pts": state.get("cbbc_nearest_bull_distance_pts"),
        "nearest_bear_distance_pts": state.get("cbbc_nearest_bear_distance_pts"),
    }


async def request_hksi_style_advice(
    *,
    zones: Mapping[str, Any],
    state: Mapping[str, Any],
    regime: Optional[Mapping[str, Any]],
    klines: list[Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    api_style: str,
    timeout_seconds: float = 45.0,
    max_tokens: int = 3500,
) -> HksiStyleAdviceResult:
    context = build_context(zones=zones, state=state, regime=regime, klines=klines)
    if not api_key:
        return HksiStyleAdviceResult(ok=False, error="未配置 AI api_key", model=model, context=context)
    if not base_url:
        return HksiStyleAdviceResult(ok=False, error="未配置 AI base_url", model=model, context=context)

    user_prompt = "以下係本系統即市 HKSI-style AI 分析 context JSON：\n" + json.dumps(
        context,
        ensure_ascii=False,
        indent=2,
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
        parsed = _extract_json_object(raw_text)
        if parsed is None:
            return HksiStyleAdviceResult(
                ok=False,
                error="模型輸出無法解析為 JSON",
                raw_model_text=raw_text,
                model=used_model,
                elapsed_seconds=elapsed,
                context=context,
            )
        review = normalize_review(parsed, context)
        return HksiStyleAdviceResult(
            ok=True,
            review=review,
            context=context,
            raw_model_text=raw_text,
            model=used_model,
            elapsed_seconds=elapsed,
        )
    except Exception as exc:  # noqa: BLE001
        return HksiStyleAdviceResult(
            ok=False,
            error=f"AI 端點調用失敗: {type(exc).__name__}: {exc}",
            model=model,
            context=context,
        )


def normalize_review(review: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    plan = _normalize_trade_plan(_as_dict(review.get("trade_plan")))
    execution = _normalize_execution_plan(_as_dict(review.get("execution_plan")))
    coefficients = _normalize_trade_coefficients(_as_dict(review.get("trade_coefficients")), execution)

    risk_level = str(review.get("risk_level", "medium") or "medium").lower()
    if risk_level not in ALLOWED_RISK_LEVELS:
        risk_level = "medium"
    actionability = str(review.get("actionability", "observe") or "observe").lower()
    if actionability not in ALLOWED_ACTIONABILITY:
        actionability = "observe"

    normalized = {
        "summary": str(review.get("summary", "") or "").strip(),
        "trade_plan": plan,
        "execution_plan": execution,
        "trade_coefficients": coefficients,
        "risk_level": risk_level,
        "confidence_comment": str(review.get("confidence_comment", "") or "").strip(),
        "key_supporting_points": _string_list(review.get("key_supporting_points")),
        "conflicts": _string_list(review.get("conflicts")),
        "watch_levels": _string_list(review.get("watch_levels")),
        "data_quality_notes": _string_list(review.get("data_quality_notes")),
        "suggested_user_action": str(review.get("suggested_user_action", "") or "").strip(),
        "actionability": actionability,
        "limitations": _string_list(review.get("limitations")),
    }
    return _validate_semantics(normalized, context)


def _extract_levels_from_text(items: list[str]) -> list[float]:
    """Extract HSI-like price levels (4-5 digit) from a list of free-text strings.

    Used to auto-fill ``execution.take_profit_levels`` etc. when the model
    populated the human-readable ``trade_plan.take_profit`` block but forgot
    the parallel structured array. Catches numbers like ``25800``, ``25,850``,
    ``25400-25425``. Numbers must look like an HSI price (15000-50000) to
    be accepted.
    """
    out: list[float] = []
    for item in items:
        if not isinstance(item, str):
            continue
        for m in re.findall(r"\d{2,3}[,]?\d{3}(?:\.\d+)?", item):
            try:
                value = float(m.replace(",", ""))
            except ValueError:
                continue
            if 15000 <= value <= 50000:
                out.append(round(value, 2))
    # de-dupe and sort.
    return sorted(set(out))


def _attempt_autofix(plan: dict[str, Any], execution: dict[str, Any], coefficients: dict[str, Any]) -> list[str]:
    """Try to recover missing structured fields from prose before nuking.

    Mutates the dicts in-place. Returns a list of warning strings describing
    anything that was auto-fixed (these go into ``data_quality_notes`` so
    the user knows the validator stepped in but didn't kill the result).
    """
    notes: list[str] = []

    # 1) execution.take_profit_levels missing → derive from trade_plan.take_profit text.
    if not execution.get("take_profit_levels"):
        levels = _extract_levels_from_text(plan.get("take_profit") or [])
        if levels:
            execution["take_profit_levels"] = levels[:6]
            notes.append("auto-fix: take_profit_levels 從 trade_plan 文字提取")

    # 2) execution.stop_loss_levels missing → derive from trade_plan.stop_loss text.
    if not execution.get("stop_loss_levels"):
        levels = _extract_levels_from_text(plan.get("stop_loss") or [])
        if levels:
            execution["stop_loss_levels"] = levels[:6]
            notes.append("auto-fix: stop_loss_levels 從 trade_plan 文字提取")

    # 3) entry_rules missing or invalid → synthesize from trade_plan.entry_plan_1.
    side = execution.get("side")
    if execution.get("enabled") and side in {"bull", "bear"} and not execution.get("entry_rules"):
        ep = plan.get("entry_plan_1") or {}
        action_str = str(ep.get("action") or "").strip()
        ep_amount = max(0, _safe_int(ep.get("amount")))
        expected_action = "buy_bull" if side == "bull" else "buy_bear"
        # The model may have put the entry price into the condition text.
        levels = _extract_levels_from_text([str(ep.get("condition") or "")])
        if action_str in {"買升", "買跌"} and ep_amount > 0 and levels:
            entry = levels[0]
            execution["entry_rules"] = [{
                "label": "entry_plan_1 (auto)",
                "action": expected_action,
                "price_min": round(entry - 5, 2),
                "price_max": round(entry + 5, 2),
                "rsi_min": 0.0,
                "rsi_max": 100.0,
                "vwap_relation": "any",
                "amount": ep_amount,
                "priority": 1,
                "comment": "由 trade_plan.entry_plan_1 自動合成",
            }]
            notes.append("auto-fix: entry_rules 從 entry_plan_1 自動合成")

    # 4) coefficients direction/range missing → derive from execution side + entry rules.
    if coefficients.get("entry_min") <= 0 and execution.get("entry_rules"):
        first = execution["entry_rules"][0]
        coefficients["entry_min"] = round(_safe_float(first.get("price_min")), 2)
        coefficients["entry_max"] = round(_safe_float(first.get("price_max")), 2)
    if coefficients.get("take_profit_min") <= 0 and execution.get("take_profit_levels"):
        levels = execution["take_profit_levels"]
        coefficients["take_profit_min"] = levels[0]
        coefficients["take_profit_max"] = levels[-1]
    if coefficients.get("stop_loss_min") <= 0 and execution.get("stop_loss_levels"):
        levels = execution["stop_loss_levels"]
        coefficients["stop_loss_min"] = levels[0]
        coefficients["stop_loss_max"] = levels[-1]
    if coefficients.get("direction") == "NEUTRAL" and side in {"bull", "bear"}:
        coefficients["direction"] = "UP" if side == "bull" else "DOWN"

    return notes


def _validate_semantics(review: dict[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    plan = review["trade_plan"]
    execution = review["execution_plan"]
    coefficients = review["trade_coefficients"]
    status = plan.get("status")
    side = execution.get("side")
    direction = coefficients.get("direction")

    # 軟弃單路徑: status="不交易"/"等待確認" 時已經明確不入場,直接清空執行計劃
    # 但保留 trade_plan 的人類分析,前端正常顯示。
    if status in {"不交易", "等待確認"}:
        execution["enabled"] = False
        execution["side"] = "none"
        execution["entry_rules"] = []
        execution["max_position_hkd"] = 0
        coefficients.update({"direction": "NEUTRAL", "share_count": 0})
        for key in ("entry_plan_1", "entry_plan_2"):
            plan[key]["amount"] = 0
            if plan[key]["action"] not in ALLOWED_ACTIONS:
                plan[key]["action"] = "不做"
        if not plan.get("product_warning"):
            plan["product_warning"] = "牛熊證避免太近收回價，需預留足夠安全距離。"
        if not plan.get("give_up_conditions"):
            plan["give_up_conditions"] = ["VWAP 方向轉差", "CBBC 密集區或磁吸訊號轉向", "market regime 轉中性或相反方向"]
        if not review.get("summary"):
            review["summary"] = plan.get("summary") or "方向未完全確認，先以觀察及風險控制為主。"
        return review

    # Hard path: execution.enabled=true,要求結構完整且自洽。
    # 之前的版本只要 take_profit_levels / stop_loss_levels / entry_rules 任一缺失就一刀切到"不交易",
    # 但模型常常只把數字寫進 trade_plan 文字而忘了結構數組 — 那種情況下用 _attempt_autofix 從文字提取。
    autofix_notes: list[str] = []
    if execution.get("enabled"):
        autofix_notes = _attempt_autofix(plan, execution, coefficients)
        side = execution.get("side")
        direction = coefficients.get("direction")

    problems: list[str] = []
    if execution.get("enabled"):
        expected_action = {"bull": "buy_bull", "bear": "buy_bear"}.get(side)
        expected_direction = {"bull": "UP", "bear": "DOWN"}.get(side, "NEUTRAL")
        if side not in {"bull", "bear"}:
            problems.append("execution_plan.enabled=true 但 side 不是 bull/bear")
        if direction != expected_direction:
            # 嘗試自動修復 direction/side 對齊
            coefficients["direction"] = expected_direction
            autofix_notes.append("auto-fix: trade_coefficients.direction 對齊到 execution.side")
        valid_rules = []
        for rule in execution.get("entry_rules", []):
            if (
                rule.get("action") == expected_action
                and rule.get("amount", 0) > 0
                and rule.get("price_min", 0) > 0
                and rule.get("price_max", 0) > 0
            ):
                valid_rules.append(rule)
        execution["entry_rules"] = valid_rules[:4]
        if not valid_rules:
            problems.append("execution_plan.enabled=true 但無有效 entry rule")
        if not execution.get("take_profit_levels"):
            problems.append("execution_plan.enabled=true 但無 take_profit_levels")
        if not execution.get("stop_loss_levels") and not execution.get("give_up_levels"):
            problems.append("execution_plan.enabled=true 但無 stop_loss_levels/give_up_levels")

    if problems:
        # 仍然有結構性問題 (autofix 救不回來) → 強制降級到"不交易",
        # 但保留 trade_plan 的人類可讀分析以便排查。
        execution.update({"enabled": False, "side": "none", "entry_rules": [], "max_position_hkd": 0})
        coefficients.update({"direction": "NEUTRAL", "share_count": 0})
        plan["status"] = "不交易"
        review["actionability"] = "avoid_trade"
        review["risk_level"] = "high"
        review["conflicts"] = (review.get("conflicts") or []) + problems
        review["limitations"] = (review.get("limitations") or []) + [
            "AI 輸出未通過本系統 HKSI-style 語義驗證，已強制轉為不交易。"
        ]
    elif autofix_notes:
        # autofix 救活了 → 把警告寫到 data_quality_notes,不影響交易計劃。
        review["data_quality_notes"] = (
            review.get("data_quality_notes") or []
        ) + autofix_notes

    if not plan.get("product_warning"):
        plan["product_warning"] = "牛熊證避免太近收回價，需預留足夠安全距離。"
    if not plan.get("give_up_conditions"):
        plan["give_up_conditions"] = ["VWAP 方向轉差", "CBBC 密集區或磁吸訊號轉向", "market regime 轉中性或相反方向"]
    if not review.get("summary"):
        review["summary"] = plan.get("summary") or "方向未完全確認，先以觀察及風險控制為主。"
    return review


def _normalize_trade_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    direction = str(plan.get("main_direction", "中性 [neutral]") or "中性 [neutral]").strip()
    allowed_directions = {"偏升 [up]", "中性偏升 [up]", "中性 [neutral]", "中性偏跌 [down]", "偏跌 [down]"}
    if direction not in allowed_directions:
        direction = "中性 [neutral]"
    status = str(plan.get("status", "不交易") or "不交易").strip()
    if status not in ALLOWED_STATUS:
        status = "不交易"
    return {
        "main_direction": direction,
        "status": status,
        "entry_plan_1": _normalize_entry_plan(_as_dict(plan.get("entry_plan_1"))),
        "entry_plan_2": _normalize_entry_plan(_as_dict(plan.get("entry_plan_2"))),
        "stop_loss": _string_list(plan.get("stop_loss")),
        "take_profit": _string_list(plan.get("take_profit")),
        "give_up_conditions": _string_list(plan.get("give_up_conditions")),
        "product_warning": str(plan.get("product_warning", "") or "").strip(),
        "summary": str(plan.get("summary", "") or "").strip(),
    }


def _normalize_entry_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    action = str(plan.get("action", "不做") or "不做").strip()
    return {
        "condition": str(plan.get("condition", "") or "").strip(),
        "action": action if action in ALLOWED_ACTIONS else "不做",
        "amount": max(0, _safe_int(plan.get("amount"))),
    }


def _normalize_execution_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    side = str(plan.get("side", "none") or "none").lower()
    if side not in ALLOWED_SIDES:
        side = "none"
    return {
        "enabled": bool(plan.get("enabled")) and side in {"bull", "bear"},
        "side": side,
        "entry_rules": [_normalize_execution_rule(_as_dict(rule)) for rule in _list(plan.get("entry_rules"))][:4],
        "take_profit_levels": _float_list(plan.get("take_profit_levels")),
        "stop_loss_levels": _float_list(plan.get("stop_loss_levels")),
        "give_up_levels": _float_list(plan.get("give_up_levels")),
        "max_position_hkd": max(0, _safe_int(plan.get("max_position_hkd"))),
        "time_in_force": str(plan.get("time_in_force", "intraday") or "intraday").strip()[:40],
        "notes": str(plan.get("notes", "") or "").strip()[:240],
    }


def _normalize_execution_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    action = str(rule.get("action", "none") or "none").lower()
    relation = str(rule.get("vwap_relation", "any") or "any").lower()
    low, high = sorted((_safe_float(rule.get("price_min")), _safe_float(rule.get("price_max"))))
    return {
        "label": str(rule.get("label", "") or "").strip()[:80],
        "action": action if action in ALLOWED_EXECUTION_ACTIONS else "none",
        "price_min": round(max(0.0, low), 2),
        "price_max": round(max(0.0, high), 2),
        "rsi_min": round(_clamp(_safe_float(rule.get("rsi_min")), 0, 100), 2),
        "rsi_max": round(_clamp(_safe_float(rule.get("rsi_max"), 100), 0, 100), 2),
        "vwap_relation": relation if relation in ALLOWED_VWAP_RELATIONS else "any",
        "amount": max(0, _safe_int(rule.get("amount"))),
        "priority": max(1, min(9, _safe_int(rule.get("priority"), 1))),
        "comment": str(rule.get("comment", "") or "").strip()[:160],
    }


def _normalize_trade_coefficients(plan: Mapping[str, Any], execution: Mapping[str, Any]) -> dict[str, Any]:
    direction = str(plan.get("direction", "NEUTRAL") or "NEUTRAL").upper()
    if direction not in ALLOWED_COEFFICIENT_DIRECTIONS:
        direction = {"bull": "UP", "bear": "DOWN"}.get(str(execution.get("side", "none")), "NEUTRAL")
    entry_min, entry_max = _range_pair(plan.get("entry_min"), plan.get("entry_max"))
    take_profit_min, take_profit_max = _range_pair(plan.get("take_profit_min"), plan.get("take_profit_max"))
    stop_loss_min, stop_loss_max = _range_pair(plan.get("stop_loss_min"), plan.get("stop_loss_max"))
    if entry_min <= 0 and execution.get("entry_rules"):
        first = execution["entry_rules"][0]
        entry_min, entry_max = _range_pair(first.get("price_min"), first.get("price_max"))
    if take_profit_min <= 0:
        take_profit_min, take_profit_max = _levels_pair(execution.get("take_profit_levels"))
    if stop_loss_min <= 0:
        stop_loss_min, stop_loss_max = _levels_pair(execution.get("stop_loss_levels") or execution.get("give_up_levels"))
    return {
        "direction": direction,
        "entry_min": round(entry_min, 2),
        "entry_max": round(entry_max, 2),
        "share_count": max(0, _safe_int(plan.get("share_count"))),
        "take_profit_min": round(take_profit_min, 2),
        "take_profit_max": round(take_profit_max, 2),
        "stop_loss_min": round(stop_loss_min, 2),
        "stop_loss_max": round(stop_loss_max, 2),
        "confidence": round(_clamp(_safe_float(plan.get("confidence")), 0, 1), 3),
        "notes": str(plan.get("notes", "") or "").strip()[:240],
    }


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:10]
    text = str(value or "").strip()
    return [text] if text else []


def _float_list(value: Any) -> list[float]:
    items = []
    for item in _list(value):
        number = _safe_float(item)
        if number > 0:
            items.append(round(number, 2))
    return sorted({item for item in items})[:10]


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        value = match.group(0) if match else text
    try:
        number = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _safe_int(value: Any, default: int = 0) -> int:
    return int(_safe_float(value, float(default)))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _range_pair(left: Any, right: Any) -> tuple[float, float]:
    low = max(0.0, _safe_float(left))
    high = max(0.0, _safe_float(right))
    if low > 0 and high > 0 and low > high:
        low, high = high, low
    return low, high


def _levels_pair(value: Any) -> tuple[float, float]:
    levels = _float_list(value)
    if not levels:
        return 0.0, 0.0
    if len(levels) == 1:
        return levels[0], levels[0]
    return levels[0], levels[-1]


__all__ = ["HksiStyleAdviceResult", "request_hksi_style_advice", "build_context"]
