"""Collect public Kalshi trades into SQLite."""

import argparse
from base64 import b64encode
from contextlib import closing, nullcontext
import json
import logging
import os
from pathlib import Path
from typing import TextIO
import sqlite3
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from kalshi_replay import CREATE_TRADES, Trade, print_summary, store_trade


WS_PATH = "/trade-api/ws/v2"
WS_URL = "wss://external-api-ws.kalshi.com" + WS_PATH
DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co" + WS_PATH
LOG = logging.getLogger(__name__)


def open_series_markets(series: str, *, demo: bool = False) -> list[tuple[str, str]]:
    """Return the ticker and Yes label for each currently open market."""
    host = "external-api.demo.kalshi.co" if demo else "external-api.kalshi.com"
    params = {"series_ticker": series, "status": "open"}
    markets = []
    while True:
        url = f"https://{host}/trade-api/v2/markets?{urlencode(params)}"
        with urlopen(url, timeout=10) as response:
            page = json.load(response)
        markets.extend(
            (market["ticker"], market.get("yes_sub_title") or market["ticker"])
            for market in page["markets"]
        )
        cursor = page.get("cursor")
        if not cursor:
            return markets
        params["cursor"] = cursor


def auth_headers(key_id: str, private_key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey) -> dict[str, str]:
    timestamp = str(time.time_ns() // 1_000_000)
    message = f"{timestamp}GET{WS_PATH}".encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        digest = hashes.SHA256()
        signature = private_key.sign(
            message, padding.PSS(mgf=padding.MGF1(digest), salt_length=digest.digest_size), digest
        )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": b64encode(signature).decode("ascii"),
    }


def read_trades(socket: ClientConnection, database: sqlite3.Connection,
                quotes: TextIO | None = None) -> None:
    while True:
        try:
            raw_message = socket.recv(timeout=1)
        except TimeoutError:
            continue  # Wake periodically so Ctrl+C can stop an idle feed on Windows.

        try:
            message = json.loads(raw_message)
            if not isinstance(message, dict):
                raise ValueError("expected a JSON object")
            message_type = message.get("type")
            if message_type == "trade":
                trade = Trade.from_payload(message.get("msg"))
                if store_trade(database, trade):
                    LOG.info("Stored %s  %s  %s",
                             trade.trade_id, trade.market_ticker, trade.trade_time_utc)
            elif message_type == "ticker" and quotes is not None:
                payload = message.get("msg")
                if not isinstance(payload, dict):
                    raise ValueError("expected a ticker payload")
                print(json.dumps(payload), file=quotes, flush=True)
            elif message_type == "subscribed":
                LOG.info("Subscribed to public trades. Waiting for new trades.")
            elif message_type == "error":
                raise RuntimeError(f"Kalshi rejected the subscription: {message.get('msg')}")
        except ValueError as exc:
            LOG.warning("Skipped malformed message: %s", exc)


def collect(database: sqlite3.Connection, tickers: list[str], key_id: str,
            private_key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey, *,
            url: str = WS_URL, quotes: TextIO | None = None) -> None:
    database.execute(CREATE_TRADES)
    channels = ['trade', 'ticker'] if quotes is not None else ['trade']
    subscription = {
        "id": 1,
        "cmd": "subscribe",
        "params": {"channels": channels, "market_tickers": tickers},
    }
    while True:
        try:
            with connect(url, additional_headers=auth_headers(key_id, private_key)) as socket:
                socket.send(json.dumps(subscription))
                read_trades(socket, database, quotes)
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status == 401:
                environment = "demo" if url == DEMO_WS_URL else "production"
                try:
                    error = json.loads(exc.response.body or b"{}")
                except ValueError:
                    error = {}
                if isinstance(error, dict) and error.get("details") == "NOT_FOUND":
                    hint = "Kalshi did not recognize the API key ID (NOT_FOUND)."
                else:
                    hint = "Check that the key ID and private key are a matching pair for this environment."
                raise RuntimeError(
                    f"Kalshi {environment} WebSocket handshake rejected (HTTP 401). {hint}"
                ) from exc
            if status != 429 and not 500 <= status < 600:
                raise RuntimeError(f"Kalshi WebSocket handshake rejected (HTTP {status})") from exc
            LOG.warning("Kalshi returned HTTP %s. Reconnecting in 5 seconds.", status)
        except InvalidHandshake as exc:
            raise RuntimeError("Could not complete the Kalshi WebSocket handshake") from exc
        except (ConnectionClosed, OSError) as exc:
            LOG.warning("Connection interrupted (%s). Reconnecting in 5 seconds.", exc)
        time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", metavar="MARKET_TICKER")
    parser.add_argument("--series", help="subscribe to open markets in this series")
    parser.add_argument("--demo", action="store_true", help="connect to the Kalshi demo environment")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--quotes", type=Path, help="append ticker updates to JSONL")
    args = parser.parse_args()
    if bool(args.series) == bool(args.tickers):
        parser.error("provide either --series SERIES_TICKER or one or more market tickers")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    tickers = args.tickers
    if args.series:
        series = args.series.strip().upper()
        try:
            markets = open_series_markets(series, demo=args.demo)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.exit(1, f"Could not look up markets for {series}: {exc}\n")
        if not markets:
            parser.exit(1, f"No open markets found for {series}.\n")
        tickers = [ticker for ticker, _ in markets]
        LOG.info("Found %d open markets for %s", len(markets), series)
        for ticker, label in markets:
            LOG.info("  %s  %s", ticker, label)

    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        parser.error("set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
    try:
        private_key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"could not load the private key: {exc}")
    if not isinstance(private_key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
        parser.error("the private key must be Ed25519 or RSA")

    url = DEMO_WS_URL if args.demo else WS_URL
    default_db = "weather_demo.sqlite3" if args.demo else "weather_live.sqlite3"
    db_path = args.db if args.db is not None else default_db
    if args.quotes and args.quotes.resolve() == Path(db_path).resolve():
        parser.error("--quotes and --db must use different files")
    recording = args.quotes.open("a", encoding="utf-8") if args.quotes else nullcontext()
    with recording as quotes, closing(sqlite3.connect(db_path)) as database:
        try:
            collect(database, tickers, key_id, private_key, url=url, quotes=quotes)
        except KeyboardInterrupt:
            print_summary(database)
        except RuntimeError as exc:
            parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
