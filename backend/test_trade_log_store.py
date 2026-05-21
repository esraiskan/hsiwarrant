import tempfile
import unittest
from pathlib import Path

from trade_log_store import _today_hk, load_today_trade_log


class TradeLogStoreTest(unittest.TestCase):
    def test_load_today_trade_log_handles_utf8_bom_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trade_log.csv"
            path.write_text(
                "\ufeff\"time\",\"signal\",\"price\",\"rsi\",\"position\",\"pnl\",\"pnl_hkd\",\"message\"\n"
                f"\"{_today_hk()} 09:31:00\",\"buy_bull\",\"26000\",\"25\",\"bull\",\"\",\"\",\"test\"\n",
                encoding="utf-8",
            )

            records = load_today_trade_log(path)

        self.assertEqual(1, len(records))
        self.assertEqual(f"{_today_hk()} 09:31:00", records[0].time)


if __name__ == "__main__":
    unittest.main()
