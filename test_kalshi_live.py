from base64 import b64decode
from contextlib import closing
from io import BytesIO
import json
import sqlite3
import unittest
from importlib.util import find_spec
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from kalshi_replay import DEFAULT_SAMPLE, MINUTE_SUMMARY

LIVE_INSTALLED = all(find_spec(name) for name in ("cryptography", "websockets"))

if LIVE_INSTALLED:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from websockets.exceptions import InvalidStatus

    from kalshi_live import DEMO_WS_URL, WS_URL, collect, open_series_markets


@unittest.skipUnless(LIVE_INSTALLED, "install requirements-live.txt to test live mode")
class LiveTests(unittest.TestCase):
    def test_demo_auth_failure_uses_demo_host_and_explains_key_mismatch(self):
        key = Ed25519PrivateKey.generate()
        cases = [
            (b'{"code":"authentication_error","details":"NOT_FOUND"}', "NOT_FOUND"),
            (b"<html>Unauthorized</html>", "matching pair"),
        ]
        for body, hint in cases:
            with self.subTest(hint=hint), closing(sqlite3.connect(":memory:")) as database:
                response = MagicMock(status_code=401, body=body)
                with patch("kalshi_live.connect", side_effect=InvalidStatus(response)) as connect:
                    with self.assertRaisesRegex(RuntimeError, hint):
                        collect(database, ["DEMO-MARKET"], "test-key-id", key, url=DEMO_WS_URL)
                connect.assert_called_once()
                self.assertEqual(
                    connect.call_args.args,
                    ("wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",),
                )

    def test_series_lookup_follows_the_cursor(self):
        pages = [
            {"markets": [{"ticker": "TEST-A", "yes_sub_title": "80° to 81°"}], "cursor": "more+data"},
            {"markets": [{"ticker": "TEST-B", "yes_sub_title": "82° to 83°"}], "cursor": ""},
        ]
        responses = [closing(BytesIO(json.dumps(page).encode())) for page in pages]
        with patch("kalshi_live.urlopen", side_effect=responses) as request:
            markets = open_series_markets("TEST", demo=True)

        self.assertEqual(markets, [("TEST-A", "80° to 81°"), ("TEST-B", "82° to 83°")])
        first, second = [urlsplit(call.args[0]) for call in request.call_args_list]
        self.assertEqual(first.netloc, "external-api.demo.kalshi.co")
        self.assertEqual(parse_qs(first.query), {"series_ticker": ["TEST"], "status": ["open"]})
        self.assertEqual(parse_qs(second.query)["cursor"], ["more+data"])

    def test_series_lookup_can_return_no_open_markets(self):
        response = closing(BytesIO(b'{"markets": [], "cursor": ""}'))
        with patch("kalshi_live.urlopen", return_value=response):
            self.assertEqual(open_series_markets("TEST"), [])

    def test_reconnect_resubscribes_and_does_not_duplicate_trades(self):
        key = Ed25519PrivateKey.generate()
        payload = json.loads(DEFAULT_SAMPLE.read_text(encoding="utf-8").splitlines()[0])
        trade_message = json.dumps({"type": "trade", "msg": payload})
        ticker = payload["market_ticker"]
        first_socket = MagicMock()
        first_socket.__enter__.return_value.recv.side_effect = [
            json.dumps({"type": "subscribed"}),
            "{broken",
            trade_message,
            OSError("disconnected"),
        ]
        second_socket = MagicMock()
        second_socket.__enter__.return_value.recv.side_effect = [
            trade_message,
            KeyboardInterrupt,
        ]

        with closing(sqlite3.connect(":memory:")) as database:
            with patch("kalshi_live.connect", side_effect=[first_socket, second_socket]) as connect:
                with patch("kalshi_live.time.sleep") as sleep:
                    with self.assertLogs("kalshi_live", level="WARNING") as logs:
                        with self.assertRaises(KeyboardInterrupt):
                            collect(database, [ticker], "test-key-id", key)

            self.assertEqual(database.execute(MINUTE_SUMMARY).fetchall(),
                             [("2026-08-01T14:00", ticker, 1, 10.0)])

        sleep.assert_called_once_with(5)
        self.assertTrue(any("Skipped malformed message" in line for line in logs.output))
        self.assertEqual(connect.call_count, 2)
        for call, socket in zip(connect.call_args_list, [first_socket, second_socket]):
            self.assertEqual(call.args, (WS_URL,))
            headers = call.kwargs["additional_headers"]
            self.assertEqual(headers["KALSHI-ACCESS-KEY"], "test-key-id")
            timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]
            signed = f"{timestamp}GET/trade-api/ws/v2".encode()
            key.public_key().verify(b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), signed)
            socket.__enter__.return_value.send.assert_called_once()
            subscription = json.loads(socket.__enter__.return_value.send.call_args.args[0])
            self.assertEqual(subscription["cmd"], "subscribe")
            self.assertEqual(subscription["params"], {
                "channels": ["trade"], "market_tickers": [ticker],
            })
