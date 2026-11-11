# Kalshi Weather Trade Monitor

A Python pipeline that streams public trades for weather markets into SQLite. The SQL report shows trade count and contracts traded per market and UTC minute.

## Run the demo

From the project directory, with Python 3.10 or newer:

```sh
python kalshi_replay.py --db ":memory:"
```

The replay uses Python's standard library and five bundled synthetic messages. It runs offline without an account or API key. Four unique trades produce the following report.

| Minute (UTC) | Market | Trades | Contracts |
|---|---|---:|---:|
| 2026-08-01 14:00 | 80–81°F | 2 | 15.00 |
| 2026-08-01 14:00 | 82–83°F | 1 | 7.00 |
| 2026-08-01 14:01 | 80–81°F | 1 | 3.00 |

One 14:00 trade arrives after a 14:01 trade. It still goes into the 14:00 bucket because the report uses the time the trade occurred. The repeated trade ID counts once. To verify that a second replay leaves the totals unchanged, run twice with the same file path in `--db`.

## How it works

```text
JSONL replay or Kalshi WebSocket → Python validation → SQLite trades → SQL minute summary
```

Live mode uses Kalshi's REST API to find open markets for a series, then subscribes to their trade messages over WebSocket. Replay mode reads the same kind of trade payload from JSONL. Both paths use the same Python validation, SQLite table, and SQL report.

## Run against Kalshi

Live mode uses Kalshi's public trades WebSocket with a signed connection. Create a production API key in Kalshi and save the private key file outside this repository. The script supports Ed25519 and RSA keys. [Kalshi API key instructions](https://docs.kalshi.com/getting_started/api_keys)

In PowerShell, set the key ID and private key path for the current terminal and run the collector.

```powershell
py -m pip install -r requirements-live.txt
$env:KALSHI_API_KEY_ID = '<your key ID>'
$env:KALSHI_PRIVATE_KEY_PATH = 'C:\path\to\your\kalshi.key'
py kalshi_live.py --series KXHIGHLAX
```

With `--series`, the collector looks up open markets at startup and prints the tickers and temperature ranges it will watch. Restart it to pick up new markets as they open. To watch specific markets, pass their market tickers as positional arguments.

The collector prints trades as they arrive and saves them to `weather_live.sqlite3`. Press Ctrl+C to stop and print the minute summary. An idle market can go a while without a trade. After a dropped connection the collector reconnects and resubscribes, so trades during the gap may be missing.

### Use the Kalshi sandbox

For a key issued by Kalshi's demo environment, set the same two environment variables using the demo key ID and its matching private key file. Then use a market ticker from the [demo markets API](https://external-api.demo.kalshi.co/trade-api/v2/markets?status=open&limit=20).

```powershell
py kalshi_live.py --demo '<open demo market ticker>'
```

Demo trades are stored in `weather_demo.sqlite3` by default. If the handshake returns HTTP 401, check that the key ID and private key come from the same key pair and that the key was issued for the environment you selected. [Kalshi API environments](https://docs.kalshi.com/getting_started/api_environments)

## Run the tests

Install the dependencies in equirements-live.txt to run the full suite, including the WebSocket tests. The tests use simulated messages and need no credentials.

```sh
python -m unittest -v
```
