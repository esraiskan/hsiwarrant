"""
Task 1.2 单元测试：``runtime_config_store`` CBBC 字段。

覆盖：
- 默认值（cbbc-magnet-signal R9.3 / R9.4）
- 合法值与区间边界
- 越界 / NaN / ±Inf / 错误类型 → 回退默认 + WARN 日志（R9.6, R9.9）
- 非对象根 / 非法 JSON / 缺失文件（R9.9）
- save_runtime_config 原子写入失败回滚（R9.8）
- 往返一致性
"""
from __future__ import annotations

import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from runtime_config_store import (
    CBBC_FIELD_DEFAULTS,
    load_runtime_config,
    save_runtime_config,
)


# 真值表：固定的字段 → (default, low, high, low_inclusive, high_inclusive)
_FLOAT_FIELDS: dict[str, tuple[float, float, float, bool, bool]] = {
    "cbbc_magnet_decay_points": (300.0, 0.0, 10000.0, False, True),
    "cbbc_dense_band_threshold_pts": (150.0, 10.0, 1000.0, True, True),
    "cbbc_dense_band_pull_share": (0.40, 0.0, 1.0, True, True),
    "cbbc_intraday_poll_interval_seconds": (60.0, 10.0, 600.0, True, True),
    "cbbc_magnet_direction_gate_threshold": (0.15, 0.0, 1.0, True, True),
}

_BOOL_FIELDS: tuple[str, ...] = (
    "cbbc_magnet_layer_enabled",
    "cbbc_intraday_polling_suspended",
    "cbbc_magnet_direction_gate_enabled",
    "cbbc_ai_advisor_enabled",
)

_WARN_MARK = "event=config_corrupt_fallback_defaults"


@contextmanager
def _capture_stdout():
    """暂时把 ``sys.stdout`` 替换为 ``StringIO``，捕获 ``print`` 输出。"""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TempPathMixin:
    """每个测试用例独立的临时目录。"""

    def setUp(self) -> None:  # type: ignore[override]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.cfg_path = self.tmp_dir / "runtime_config.json"

    def tearDown(self) -> None:  # type: ignore[override]
        self._tmp.cleanup()


class DefaultsTests(unittest.TestCase):
    """R9.3 / R9.4：6 个 CBBC 字段的默认值与公开导出。"""

    def test_cbbc_field_defaults_match_spec(self) -> None:
        # 完全相同：键集合 + 类型 + 数值。
        self.assertEqual(
            set(CBBC_FIELD_DEFAULTS.keys()),
            {
                "cbbc_magnet_layer_enabled",
                "cbbc_intraday_polling_suspended",
                "cbbc_magnet_decay_points",
                "cbbc_dense_band_threshold_pts",
                "cbbc_dense_band_pull_share",
                "cbbc_intraday_poll_interval_seconds",
                # UX 增强 (非 spec):磁吸方向闸门
                "cbbc_magnet_direction_gate_enabled",
                "cbbc_magnet_direction_gate_threshold",
                # UX 增强 (非 spec):AI 决策顾问
                "cbbc_ai_advisor_enabled",
                "cbbc_ai_advisor_base_url",
                "cbbc_ai_advisor_model",
                "cbbc_ai_advisor_api_key",
                "cbbc_ai_advisor_api_style",
            },
        )
        self.assertIs(CBBC_FIELD_DEFAULTS["cbbc_magnet_layer_enabled"], False)
        self.assertIs(CBBC_FIELD_DEFAULTS["cbbc_intraday_polling_suspended"], False)
        self.assertEqual(CBBC_FIELD_DEFAULTS["cbbc_magnet_decay_points"], 300.0)
        self.assertEqual(CBBC_FIELD_DEFAULTS["cbbc_dense_band_threshold_pts"], 150.0)
        self.assertEqual(CBBC_FIELD_DEFAULTS["cbbc_dense_band_pull_share"], 0.40)
        self.assertEqual(CBBC_FIELD_DEFAULTS["cbbc_intraday_poll_interval_seconds"], 60.0)
        self.assertIs(CBBC_FIELD_DEFAULTS["cbbc_magnet_direction_gate_enabled"], False)
        self.assertEqual(CBBC_FIELD_DEFAULTS["cbbc_magnet_direction_gate_threshold"], 0.15)
        self.assertIs(CBBC_FIELD_DEFAULTS["cbbc_ai_advisor_enabled"], False)
        self.assertEqual(
            CBBC_FIELD_DEFAULTS["cbbc_ai_advisor_base_url"], "http://127.0.0.1:8765"
        )
        self.assertEqual(CBBC_FIELD_DEFAULTS["cbbc_ai_advisor_model"], "claude-opus-4-7")
        self.assertEqual(CBBC_FIELD_DEFAULTS["cbbc_ai_advisor_api_key"], "")
        self.assertEqual(CBBC_FIELD_DEFAULTS["cbbc_ai_advisor_api_style"], "openai")
        # 浮点字段确为 ``float``，布尔字段确为 ``bool``。
        for name in _FLOAT_FIELDS:
            self.assertIsInstance(CBBC_FIELD_DEFAULTS[name], float, name)
        for name in _BOOL_FIELDS:
            self.assertIsInstance(CBBC_FIELD_DEFAULTS[name], bool, name)


class LoadValidValueTests(TempPathMixin, unittest.TestCase):
    """R9.3：合法值（含区间边界）应原样返回，且不写 WARN 日志。"""

    def test_loads_typical_valid_values(self) -> None:
        payload = {
            "cbbc_magnet_layer_enabled": True,
            "cbbc_intraday_polling_suspended": True,
            "cbbc_magnet_decay_points": 250.5,
            "cbbc_dense_band_threshold_pts": 200.0,
            "cbbc_dense_band_pull_share": 0.55,
            "cbbc_intraday_poll_interval_seconds": 120.0,
        }
        _write_json(self.cfg_path, payload)
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        for k, v in payload.items():
            self.assertEqual(loaded[k], v, k)
        self.assertNotIn(_WARN_MARK, buf.getvalue())

    def test_loads_inclusive_upper_boundaries(self) -> None:
        # decay (0,10000]: 10000 inclusive; threshold [10,1000]: 1000;
        # pull_share [0,1]: 1.0; poll [10,600]: 600.0.
        payload = {
            "cbbc_magnet_decay_points": 10000.0,
            "cbbc_dense_band_threshold_pts": 1000.0,
            "cbbc_dense_band_pull_share": 1.0,
            "cbbc_intraday_poll_interval_seconds": 600.0,
        }
        _write_json(self.cfg_path, payload)
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        for k, v in payload.items():
            self.assertEqual(loaded[k], v)
        self.assertNotIn(_WARN_MARK, buf.getvalue())

    def test_loads_inclusive_lower_boundaries(self) -> None:
        # threshold low 10.0 inclusive, pull_share low 0.0 inclusive,
        # poll low 10.0 inclusive (decay low 0.0 is EXCLUSIVE — separately tested).
        payload = {
            "cbbc_dense_band_threshold_pts": 10.0,
            "cbbc_dense_band_pull_share": 0.0,
            "cbbc_intraday_poll_interval_seconds": 10.0,
        }
        _write_json(self.cfg_path, payload)
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        for k, v in payload.items():
            self.assertEqual(loaded[k], v)
        self.assertNotIn(_WARN_MARK, buf.getvalue())

    def test_loads_int_as_float_field(self) -> None:
        # JSON 整数视为浮点字段的合法值（``isinstance(int, (int, float))``）。
        payload = {"cbbc_magnet_decay_points": 300}
        _write_json(self.cfg_path, payload)
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        self.assertEqual(loaded["cbbc_magnet_decay_points"], 300.0)
        self.assertIsInstance(loaded["cbbc_magnet_decay_points"], float)
        self.assertNotIn(_WARN_MARK, buf.getvalue())


class LoadOutOfRangeTests(TempPathMixin, unittest.TestCase):
    """R9.3 / R9.6：越界（含开区间端点）→ 回退默认 + WARN。"""

    def _assert_falls_back(
        self,
        field: str,
        value: Any,
        expected_default: Any,
        expected_reason: str,
    ) -> None:
        _write_json(self.cfg_path, {field: value})
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        out = buf.getvalue()
        self.assertEqual(loaded[field], expected_default, f"{field}={value!r}")
        self.assertIn(_WARN_MARK, out)
        self.assertIn(f"field={field}", out)
        self.assertIn(f"reason={expected_reason}", out)

    def test_decay_zero_is_out_of_range_open_lower(self) -> None:
        # (0.0, 10000.0] — 0.0 在开区间外。
        self._assert_falls_back(
            "cbbc_magnet_decay_points", 0.0, 300.0, "out_of_range",
        )

    def test_decay_negative_is_out_of_range(self) -> None:
        self._assert_falls_back(
            "cbbc_magnet_decay_points", -1.0, 300.0, "out_of_range",
        )

    def test_decay_above_max(self) -> None:
        self._assert_falls_back(
            "cbbc_magnet_decay_points", 10001.0, 300.0, "out_of_range",
        )

    def test_threshold_below_min(self) -> None:
        self._assert_falls_back(
            "cbbc_dense_band_threshold_pts", 9.9, 150.0, "out_of_range",
        )

    def test_threshold_above_max(self) -> None:
        self._assert_falls_back(
            "cbbc_dense_band_threshold_pts", 1000.1, 150.0, "out_of_range",
        )

    def test_pull_share_negative(self) -> None:
        self._assert_falls_back(
            "cbbc_dense_band_pull_share", -0.01, 0.40, "out_of_range",
        )

    def test_pull_share_above_one(self) -> None:
        self._assert_falls_back(
            "cbbc_dense_band_pull_share", 1.1, 0.40, "out_of_range",
        )

    def test_poll_interval_below_min(self) -> None:
        self._assert_falls_back(
            "cbbc_intraday_poll_interval_seconds", 9.9, 60.0, "out_of_range",
        )

    def test_poll_interval_above_max(self) -> None:
        self._assert_falls_back(
            "cbbc_intraday_poll_interval_seconds", 600.1, 60.0, "out_of_range",
        )


class LoadNonFiniteTests(TempPathMixin, unittest.TestCase):
    """R9.6：NaN / ±Inf 对每个浮点字段一律回退。"""

    def test_each_float_field_rejects_non_finite(self) -> None:
        # JSON 标准并不允许 NaN/Infinity，但 Python ``json.load`` 默认会接受。
        # 我们直接构造文本以确保解析路径返回这些非有限值，模拟历史损坏文件。
        for field, (default, *_rest) in _FLOAT_FIELDS.items():
            for token in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(field=field, token=token):
                    self.cfg_path.write_text(
                        '{"' + field + '": ' + token + "}",
                        encoding="utf-8",
                    )
                    with _capture_stdout() as buf:
                        loaded = load_runtime_config(self.cfg_path)
                    out = buf.getvalue()
                    self.assertEqual(loaded[field], default)
                    self.assertIn(_WARN_MARK, out)
                    self.assertIn(f"field={field}", out)
                    self.assertIn("reason=non_finite", out)
                    # 确认默认值本身仍是有限的。
                    self.assertTrue(math.isfinite(loaded[field]))


class LoadWrongTypeTests(TempPathMixin, unittest.TestCase):
    """R9.6：错误类型 → 回退 + WARN（含 ``field=`` 字段标记）。"""

    def test_float_field_rejects_string(self) -> None:
        _write_json(self.cfg_path, {"cbbc_magnet_decay_points": "300"})
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        self.assertEqual(loaded["cbbc_magnet_decay_points"], 300.0)
        out = buf.getvalue()
        self.assertIn(_WARN_MARK, out)
        self.assertIn("field=cbbc_magnet_decay_points", out)
        self.assertIn("reason=type_mismatch", out)

    def test_float_field_rejects_list_and_dict(self) -> None:
        for raw in ([300.0], {"value": 300.0}):
            with self.subTest(raw=raw):
                _write_json(self.cfg_path, {"cbbc_dense_band_threshold_pts": raw})
                with _capture_stdout() as buf:
                    loaded = load_runtime_config(self.cfg_path)
                self.assertEqual(loaded["cbbc_dense_band_threshold_pts"], 150.0)
                out = buf.getvalue()
                self.assertIn(_WARN_MARK, out)
                self.assertIn("field=cbbc_dense_band_threshold_pts", out)

    def test_float_field_rejects_bool_disguised_as_number(self) -> None:
        # ``isinstance(True, int)`` 为真，但布尔不应被当作浮点配置接受。
        for raw in (True, False):
            with self.subTest(raw=raw):
                _write_json(self.cfg_path, {"cbbc_dense_band_pull_share": raw})
                with _capture_stdout() as buf:
                    loaded = load_runtime_config(self.cfg_path)
                self.assertEqual(loaded["cbbc_dense_band_pull_share"], 0.40)
                self.assertIn(_WARN_MARK, buf.getvalue())

    def test_bool_field_rejects_int(self) -> None:
        # 整数 1 / 0 不应被当作 ``cbbc_magnet_layer_enabled`` 的合法值。
        for raw in (1, 0, "true"):
            with self.subTest(raw=raw):
                _write_json(self.cfg_path, {"cbbc_magnet_layer_enabled": raw})
                with _capture_stdout() as buf:
                    loaded = load_runtime_config(self.cfg_path)
                self.assertIs(loaded["cbbc_magnet_layer_enabled"], False)
                out = buf.getvalue()
                self.assertIn(_WARN_MARK, out)
                self.assertIn("field=cbbc_magnet_layer_enabled", out)
                self.assertIn("reason=type_mismatch", out)


class LoadFileLevelTests(TempPathMixin, unittest.TestCase):
    """R9.9：缺失文件 / 解析失败 / 非对象根。"""

    def test_missing_file_returns_empty_no_warn(self) -> None:
        # ``self.cfg_path`` 不存在。
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        self.assertEqual(loaded, {})
        self.assertNotIn(_WARN_MARK, buf.getvalue())

    def test_corrupt_json_emits_warn_returns_empty(self) -> None:
        self.cfg_path.write_text("{not valid json", encoding="utf-8")
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        out = buf.getvalue()
        self.assertEqual(loaded, {})
        self.assertIn(_WARN_MARK, out)
        self.assertIn("reason=json_parse_failed", out)
        # 文件级警告不应携带 ``field=`` 标记。
        self.assertNotIn("field=", out)

    def test_non_object_root_emits_warn_returns_empty(self) -> None:
        for raw in ([1, 2, 3], "string", 42, True):
            with self.subTest(raw=raw):
                _write_json(self.cfg_path, raw)
                with _capture_stdout() as buf:
                    loaded = load_runtime_config(self.cfg_path)
                out = buf.getvalue()
                self.assertEqual(loaded, {})
                self.assertIn(_WARN_MARK, out)
                self.assertIn("reason=non_object_root", out)


class LoadCoexistenceTests(TempPathMixin, unittest.TestCase):
    """合法字段与非法字段共存：合法字段应被保留。"""

    def test_valid_fields_preserved_when_other_field_invalid(self) -> None:
        _write_json(
            self.cfg_path,
            {
                "cbbc_magnet_layer_enabled": True,
                "cbbc_dense_band_threshold_pts": "bad",
                "cbbc_dense_band_pull_share": 0.55,
                "unrelated_field": "kept_as_is",
            },
        )
        with _capture_stdout() as buf:
            loaded = load_runtime_config(self.cfg_path)
        self.assertIs(loaded["cbbc_magnet_layer_enabled"], True)
        self.assertEqual(loaded["cbbc_dense_band_threshold_pts"], 150.0)  # 回退
        self.assertEqual(loaded["cbbc_dense_band_pull_share"], 0.55)
        self.assertEqual(loaded["unrelated_field"], "kept_as_is")
        out = buf.getvalue()
        self.assertEqual(out.count(_WARN_MARK), 1)
        self.assertIn("field=cbbc_dense_band_threshold_pts", out)


class SaveAndRoundTripTests(TempPathMixin, unittest.TestCase):
    """save → load 往返一致；R9.5 / R9.8 写入路径校验。"""

    def test_save_then_load_preserves_values(self) -> None:
        cfg = {
            "cbbc_magnet_layer_enabled": True,
            "cbbc_intraday_polling_suspended": False,
            "cbbc_magnet_decay_points": 450.25,
            "cbbc_dense_band_threshold_pts": 175.0,
            "cbbc_dense_band_pull_share": 0.33,
            "cbbc_intraday_poll_interval_seconds": 90.0,
        }
        with _capture_stdout() as buf:
            self.assertTrue(save_runtime_config(cfg, self.cfg_path))
            loaded = load_runtime_config(self.cfg_path)
        for k, v in cfg.items():
            self.assertEqual(loaded[k], v)
        self.assertNotIn(_WARN_MARK, buf.getvalue())

    def test_save_returns_false_when_target_unwritable_directory(self) -> None:
        # 把目标 ``path`` 设成已经存在的目录，原子重命名会失败。
        # 先放一份"原始"文件用来检查回滚；目录路径独立于该文件。
        original = {"cbbc_magnet_decay_points": 300.0}
        backup_path = self.tmp_dir / "real_config.json"
        self.assertTrue(save_runtime_config(original, backup_path))

        target_dir = self.tmp_dir / "is_a_directory"
        target_dir.mkdir()
        # 在目录里放一个无关文件，确保即便原子重命名为目录也不会"成功删除"。
        (target_dir / "marker.txt").write_text("keep me", encoding="utf-8")

        with _capture_stdout():
            ok = save_runtime_config({"cbbc_magnet_decay_points": 999.0}, target_dir)
        self.assertFalse(ok)
        # 目录仍存在且未被覆盖。
        self.assertTrue(target_dir.is_dir())
        self.assertEqual(
            (target_dir / "marker.txt").read_text(encoding="utf-8"),
            "keep me",
        )
        # 备份文件未被波及（rollback 概念：上一份持久化数据保持不变）。
        with _capture_stdout():
            preserved = load_runtime_config(backup_path)
        self.assertEqual(preserved["cbbc_magnet_decay_points"], 300.0)

    def test_save_returns_false_when_atomic_replace_fails(self) -> None:
        # 写入一份有效初始配置，然后让 ``Path.replace`` 在下一次保存时抛错。
        initial = {"cbbc_magnet_decay_points": 250.0}
        self.cfg_path.write_text(
            json.dumps(initial, ensure_ascii=False), encoding="utf-8"
        )

        # 模拟磁盘满 / 权限拒绝：``tmp_path.replace(target)`` 失败。
        with mock.patch.object(Path, "replace", side_effect=OSError("disk full")):
            with _capture_stdout() as buf:
                ok = save_runtime_config(
                    {"cbbc_magnet_decay_points": 999.0}, self.cfg_path,
                )

        self.assertFalse(ok)
        # 错误信息以可观察方式打到日志（不强制具体格式）。
        self.assertIn("写入配置失败", buf.getvalue())

        # 关键：原始文件未被覆盖（atomic 写入 + 失败回滚 ≡ 原始数据完好）。
        with _capture_stdout():
            preserved = load_runtime_config(self.cfg_path)
        self.assertEqual(preserved["cbbc_magnet_decay_points"], 250.0)


class WarnLogFormatTests(TempPathMixin, unittest.TestCase):
    """R9.9：WARN 日志必须包含规范前缀字段。"""

    def test_field_level_warn_contains_required_markers(self) -> None:
        _write_json(self.cfg_path, {"cbbc_magnet_decay_points": "x"})
        with _capture_stdout() as buf:
            load_runtime_config(self.cfg_path)
        out = buf.getvalue()
        # 规范前缀 / 字段 / 来源全部齐备。
        self.assertIn("level=WARN", out)
        self.assertIn("source=runtime_config", out)
        self.assertIn(_WARN_MARK, out)
        self.assertIn("field=cbbc_magnet_decay_points", out)

    def test_file_level_warn_omits_field_marker(self) -> None:
        self.cfg_path.write_text("garbage", encoding="utf-8")
        with _capture_stdout() as buf:
            load_runtime_config(self.cfg_path)
        out = buf.getvalue()
        self.assertIn(_WARN_MARK, out)
        self.assertNotIn("field=", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
