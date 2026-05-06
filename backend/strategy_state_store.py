"""
策略运行状态持久化。
"""
import json
from pathlib import Path
from typing import Any


STRATEGY_STATE_PATH = Path(__file__).with_name("strategy_state.json")


def load_strategy_state(path: Path = STRATEGY_STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[StrategyState] 读取状态失败: {e}")
        return {}


def save_strategy_state(state: dict[str, Any], path: Path = STRATEGY_STATE_PATH) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp_path.replace(path)
        return True
    except Exception as e:
        print(f"[StrategyState] 写入状态失败: {e}")
        return False
