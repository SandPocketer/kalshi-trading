from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from kalshi_replay import DEFAULT_SAMPLE, MINUTE_SUMMARY, Trade, replay


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)

    def test_replay_uses_trade_time_and_keeps_totals_stable(self):
        expected = [
            ("2026-08-01T14:00", "DEMO-NYC-80-81F", 2, 15.0),
            ("2026-08-01T14:00", "DEMO-NYC-82-83F", 1, 7.0),
            ("2026-08-01T14:01", "DEMO-NYC-80-81F", 1, 3.0),
        ]
        with redirect_stdout(StringIO()):
            for _ in range(2):
                replay(DEFAULT_SAMPLE, self.database, delay=0)
                self.assertEqual(self.database.execute(MINUTE_SUMMARY).fetchall(), expected)

    def test_invalid_message_does_not_interrupt_replay(self):
        valid_line = DEFAULT_SAMPLE.read_text(encoding="utf-8").splitlines()[0]
        with TemporaryDirectory() as directory:
            sample = Path(directory) / "sample.jsonl"
            sample.write_text("{broken\n" + valid_line + "\n", encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                replay(sample, self.database, delay=0)
        self.assertIn("Line 1 skipped", errors.getvalue())
        self.assertEqual(self.database.execute("SELECT trade_id FROM trades").fetchall(),
                         [("t001",)])

    def test_parses_fractional_price_and_quantity(self):
        trade = Trade.from_payload({
            "trade_id": "fractional",
            "market_ticker": "DEMO-NYC-80-81F",
            "ts_ms": 1785592805000,
            "yes_price_dollars": "0.1234",
            "count_fp": "0.01",
        })
        self.assertEqual((trade.yes_price, trade.contracts), (0.1234, 0.01))


if __name__ == "__main__":
    unittest.main()
