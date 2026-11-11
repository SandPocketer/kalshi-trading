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


def record(series: list[str], directory: Path, hours: float, interval: float,
           fee: Decimal, max_mib: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    basket_path = directory / "arbitrage.jsonl"
    bytes_written = sum(p.stat().st_size for p in directory.glob("*.quotes.jsonl"))
    if basket_path.exists():
        bytes_written += basket_path.stat().st_size
    limit = max_mib * 1024 ** 2
    deadline = time.time() + hours * 3600
    health = {"series": {}, "limit_mib": max_mib, "interval_seconds": interval,
              "deadline_utc": datetime.fromtimestamp(deadline, timezone.utc).isoformat()}
    LOG.info("Collecting %s until %s; shared limit %d MiB", series, health["deadline_utc"], max_mib)
    count = cycles = 0
    full = False
    try:
        while time.time() < deadline and not full and bytes_written < limit:
            started = time.monotonic()
            for name in series:
                state = health["series"].setdefault(name, {"failures": 0})
                try:
                    events = open_events(name)
                    timestamp = time.time_ns() // 1_000_000
                    markets = [m for e in events for m in e["markets"] if m["status"] in OPEN]
                    rows = [(directory / f"{m['ticker']}.quotes.jsonl", quote_snapshot(m, timestamp)) for m in markets]
                    rows += [(basket_path, scan_basket(e, fee, timestamp)) for e in events]
                    for path, row in rows:
                        row_size = len((json.dumps(row, separators=(",", ":")) + "\n").encode())
                        if bytes_written + row_size > limit:
                            full = True
                            break
                        bytes_written += append_row(path, row)
                        count += 1
                    state.update(last_success_ms=timestamp, markets=len(markets), error=None)
                except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
                    state["failures"] += 1
                    state["error"] = str(exc)
                    LOG.warning("%s snapshot failed: %s", name, exc)
                if full:
                    break
            cycles += 1
            health.update(updated_ms=time.time_ns() // 1_000_000, bytes_stored=bytes_written, status="running")
            temporary = directory / "health.tmp"
            temporary.write_text(json.dumps(health), encoding="utf-8")
            temporary.replace(directory / "health.json")
            if cycles % 60 == 0:
                LOG.info("Saved %d rows; %.2f MiB across %d series", count, bytes_written / 1024 ** 2, len(series))
            time.sleep(min(max(0, deadline - time.time()), max(0, interval - (time.monotonic() - started))))
    finally:
        health.update(updated_ms=time.time_ns() // 1_000_000, bytes_stored=bytes_written,
                      status="storage limit" if full or bytes_written >= limit else "stopped")
        temporary = directory / "health.tmp"
        temporary.write_text(json.dumps(health), encoding="utf-8")
        temporary.replace(directory / "health.json")
        LOG.info("Collector stopped: %s; %.2f MiB", health["status"], bytes_written / 1024 ** 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", nargs="+", default=["KXHIGHLAX"], help="series tickers separated by spaces")
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
            for series in dict.fromkeys(args.series):
                for event in open_events(series.upper()):
                    print(json.dumps(scan_basket(event, args.fee, time.time_ns() // 1_000_000)))
        else:
            record(list(dict.fromkeys(s.upper() for s in args.series)), args.directory, args.hours, args.interval, args.fee, args.max_mib)
    except KeyboardInterrupt:
        LOG.info("Recording stopped.")
    except (OSError, KeyError, ValueError, InvalidOperation) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
