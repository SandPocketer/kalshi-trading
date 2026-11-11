"""Record public Kalshi quotes and scan complete weather events for paper arbitrage."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from math import isfinite
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from kalshi_arbitrage import OPEN, scan_basket


API = "https://external-api.kalshi.com/trade-api/v2"
LOG = logging.getLogger(__name__)


def open_events(series: str) -> list[dict]:
    params = {"series_ticker": series, "status": "open", "with_nested_markets": "true"}
    events = []
    while True:
        with urlopen(f"{API}/events?{urlencode(params)}", timeout=10) as response:
            page = json.load(response)
        events.extend(page["events"])
        cursor = page.get("cursor")
        if not cursor:
            return events
        params["cursor"] = cursor


def quote_snapshot(market: dict, timestamp: int) -> dict:
    return {
        "market_ticker": market["ticker"],
        "event_ticker": market["event_ticker"],
        "ts_ms": timestamp,
        "yes_bid_dollars": market["yes_bid_dollars"],
        "yes_ask_dollars": market["yes_ask_dollars"],
        "yes_bid_size_fp": market["yes_bid_size_fp"],
        "yes_ask_size_fp": market["yes_ask_size_fp"],
    }


def append_row(path: Path, row: dict) -> int:
    data = (json.dumps(row, separators=(",", ":")) + "\n").encode()
    with path.open("ab") as output:
        output.write(data)
    return len(data)


def record(series: str, directory: Path, hours: float, interval: float,
           fee: Decimal, max_mib: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    basket_path = directory / "arbitrage.jsonl"
    bytes_written = sum(path.stat().st_size for path in directory.glob("*.quotes.jsonl"))
    if basket_path.exists():
        bytes_written += basket_path.stat().st_size
    limit = max_mib * 1024 ** 2
    deadline = time.time() + hours * 3600
    LOG.info("Collecting all open %s markets until %s (limit %d MiB)", series,
             datetime.fromtimestamp(deadline, timezone.utc).isoformat(timespec="seconds"), max_mib)
    previous_tickers = set()
    count = 0
    while time.time() < deadline and bytes_written < limit:
        started = time.monotonic()
        try:
            events = open_events(series)
            timestamp = time.time_ns() // 1_000_000
            markets = [market for event in events for market in event["markets"]
                       if market["status"] in OPEN]
            tickers = {market["ticker"] for market in markets}
            if tickers != previous_tickers:
                LOG.info("Recording %d open markets across %d events", len(tickers), len(events))
                previous_tickers = tickers
            for market in markets:
                payload = quote_snapshot(market, timestamp)
                bytes_written += append_row(directory / f"{market['ticker']}.quotes.jsonl", payload)
                count += 1
            for event in events:
                result = scan_basket(event, fee, timestamp)
                bytes_written += append_row(basket_path, result)
                if result["status"] == "candidate":
                    LOG.info("Paper basket candidate %s at $%s net if filled",
                             result["event_ticker"], result["net_if_filled"])
            if markets and count % (len(markets) * 60) < len(markets):
                LOG.info("Saved %d snapshots. Quote and scan storage %.2f MiB",
                         count, bytes_written / 1024 ** 2)
        except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            LOG.warning("Missed snapshot: %s", exc)
        time.sleep(max(0, interval - (time.monotonic() - started)))
    LOG.info("Finished with %d new snapshots and %.2f MiB stored.", count, bytes_written / 1024 ** 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", default="KXHIGHLAX")
    parser.add_argument("--directory", type=Path, default=Path("recordings"))
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument("--interval", type=float, default=5, help="seconds between snapshots")
    parser.add_argument("--fee", type=Decimal, default=Decimal("0.01"), help="assumed fee per leg")
    parser.add_argument("--max-mib", type=int, default=150, help="stop when recording size reaches this limit")
    parser.add_argument("--log-file", type=Path, help="append messages to this file")
    parser.add_argument("--scan-once", action="store_true", help="print one public basket scan")
    args = parser.parse_args()
    if not isfinite(args.hours) or args.hours <= 0:
        parser.error("--hours must be finite and positive")
    if not isfinite(args.interval) or args.interval < 1:
        parser.error("--interval must be finite and at least one second")
    if not args.fee.is_finite() or args.fee < 0 or args.max_mib <= 0:
        parser.error("--fee must be finite and nonnegative, and --max-mib must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        filename=args.log_file, encoding="utf-8" if args.log_file else None)
    try:
        if args.scan_once:
            for event in open_events(args.series.upper()):
                print(json.dumps(scan_basket(event, args.fee, time.time_ns() // 1_000_000)))
        else:
            record(args.series.upper(), args.directory, args.hours, args.interval, args.fee, args.max_mib)
    except KeyboardInterrupt:
        LOG.info("Recording stopped.")
    except (OSError, KeyError, ValueError, InvalidOperation) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
