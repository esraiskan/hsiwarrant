"""
Trade Log CSV 持久化。
"""
import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models import PositionType, TradeRecord, TradeSignal


TRADE_LOG_PATH = Path(__file__).with_name("trade_log.csv")
TRADE_LOG_FIELDS = [
    "time",
    "signal",
    "price",
    "rsi",
    "position",
    "pnl",
    "pnl_hkd",
    "message",
]
HK_TZ = timezone(timedelta(hours=8), name="Asia/Hong_Kong")


def _today_hk() -> str:
    return datetime.now(HK_TZ).date().isoformat()


def _to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_optional_float(value: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_trade_log(path: Path = TRADE_LOG_PATH) -> list[TradeRecord]:
    if not path.exists():
        return []

    records: list[TradeRecord] = []
    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for line_no, row in enumerate(reader, start=2):
                try:
                    records.append(TradeRecord(
                        time=row.get("time", ""),
                        signal=TradeSignal(row.get("signal", TradeSignal.HOLD.value)),
                        price=_to_float(row.get("price", "")),
                        rsi=_to_float(row.get("rsi", "")),
                        position=PositionType(row.get("position", PositionType.NONE.value)),
                        pnl=_to_optional_float(row.get("pnl", "")),
                        pnl_hkd=_to_optional_float(row.get("pnl_hkd", "")),
                        message=row.get("message", ""),
                    ))
                except Exception as e:
                    print(f"[TradeLog] 跳过坏行 {line_no}: {e}")
    except Exception as e:
        print(f"[TradeLog] 读取 CSV 失败: {e}")
    return records


def load_today_trade_log(path: Path = TRADE_LOG_PATH) -> list[TradeRecord]:
    today = _today_hk()
    return [record for record in load_trade_log(path) if record.time[:10] == today]


def append_trade_log(record: TradeRecord, path: Path = TRADE_LOG_PATH) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow({
                "time": record.time,
                "signal": record.signal.value,
                "price": record.price,
                "rsi": record.rsi,
                "position": record.position.value,
                "pnl": "" if record.pnl is None else record.pnl,
                "pnl_hkd": "" if record.pnl_hkd is None else record.pnl_hkd,
                "message": record.message,
            })
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:
        print(f"[TradeLog] 写入 CSV 失败: {e}")
        return False
