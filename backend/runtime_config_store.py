"""
策略可调配置持久化。
"""
import json
from pathlib import Path
from typing import Any


RUNTIME_CONFIG_PATH = Path(__file__).with_name("runtime_config.json")


def load_runtime_config(path: Path = RUNTIME_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[RuntimeConfig] 读取配置失败，使用默认配置: {e}")
        return {}


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
