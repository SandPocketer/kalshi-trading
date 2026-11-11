"""Replay synthetic Kalshi trade payloads into SQLite."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from math import isfinite
from pathlib import Path
import sqlite3
import sys
import time


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DEFAULT_SAMPLE = Path(__file__).with_name("data") / "weather_trades.jsonl"

CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    market_ticker TEXT NOT NULL,
    trade_time_utc TEXT NOT NULL,
    yes_price REAL NOT NULL,
    contracts REAL NOT NULL
)
"""

MINUTE_SUMMARY = """
SELECT substr(trade_time_utc, 1, 16) AS minute_utc,
       market_ticker,
       COUNT(*) AS trade_count,
       SUM(contracts) AS contracts
FROM trades
GROUP BY minute_utc, market_ticker
ORDER BY minute_utc, market_ticker
"""


@dataclass(frozen=True)
class Trade:
    trade_id: str
    market_ticker: str
    trade_time_utc: str
    yes_price: float
    contracts: float

    @classmethod
    def from_payload(cls, payload: object) -> "Trade":
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        trade_id = payload.get("trade_id")
        market_ticker = payload.get("market_ticker")
        if not isinstance(trade_id, str) or not trade_id.strip():
            raise ValueError("trade_id must be a nonempty string")
        if not isinstance(market_ticker, str) or not market_ticker.strip():
            raise ValueError("market_ticker must be a nonempty string")

        timestamp = payload.get("ts_ms")
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("ts_ms must be a nonnegative integer")
        try:
            trade_time = EPOCH + timedelta(milliseconds=timestamp)
        except OverflowError as exc:
            raise ValueError("ts_ms is out of range") from exc

        price = payload.get("yes_price_dollars")
        quantity = payload.get("count_fp")
        if not isinstance(price, str) or not isinstance(quantity, str):
            raise ValueError("price and quantity must be numeric strings")
        price, quantity = float(price), float(quantity)
        if not 0 <= price <= 1:
            raise ValueError("price must be between $0 and $1")
        if not isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be finite and positive")

        return cls(
            trade_id, market_ticker, trade_time.isoformat(timespec="milliseconds"),
            price, quantity,
        )


def store_trade(database: sqlite3.Connection, trade: Trade) -> bool:
    with database:
        result = database.execute(
            """
            INSERT INTO trades (
                trade_id, market_ticker, trade_time_utc, yes_price, contracts
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO NOTHING
            """,
            (trade.trade_id, trade.market_ticker, trade.trade_time_utc,
             trade.yes_price, trade.contracts),
        )
    return result.rowcount == 1


def replay(sample: Path, database: sqlite3.Connection, delay: float) -> None:
    database.execute(CREATE_TRADES)
    with sample.open(encoding="utf-8") as messages:
        for line_number, line in enumerate(messages, start=1):
            if line_number > 1 and delay:
                time.sleep(delay)
            try:
                trade = Trade.from_payload(json.loads(line))
            except ValueError as exc:
                print(f"Line {line_number} skipped: {exc}", file=sys.stderr, flush=True)
                continue
            status = "Stored" if store_trade(database, trade) else "Duplicate"
            print(f"{status:9} {trade.trade_id}  {trade.market_ticker}  {trade.trade_time_utc}",
                  flush=True)

    print_summary(database)


def print_summary(database: sqlite3.Connection) -> None:
    print("\nMinute (UTC)      Market                     Trades  Contracts")
    for minute, ticker, trades, contracts in database.execute(MINUTE_SUMMARY):
        print(f"{minute}  {ticker:25}  {trades:6}  {contracts:9.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--db", default="weather.sqlite3", help="SQLite database path")
    parser.add_argument("--delay", type=float, default=0.4, help="seconds between messages")
    args = parser.parse_args()
    if not 0 <= args.delay < float("inf"):
        parser.error("--delay must be a finite, nonnegative number")
    database = sqlite3.connect(args.db)
    try:
        replay(args.sample, database, args.delay)
    finally:
        database.close()


if __name__ == "__main__":
    main()
