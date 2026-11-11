# Kalshi Weather Data & Paper Trading

A small Python data engineering project: collect weather-market quotes, replay baseline strategies, explain a linear regression filter, and view results in Streamlit. Paper-only; no orders are submitted.

## Run the demo

Use Python 3.10 or newer. From the project directory:

```sh
python -m venv .venv
```

Activate with `.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on macOS/Linux, then:

```sh
python -m pip install -r requirements.txt
python -m streamlit run kalshi_dashboard.py
```

A fresh checkout opens the synthetic demo automatically. No API credentials or collector are needed. Existing local recordings appear through the sidebar. Synthetic results illustrate the mechanics and do not establish profitability.

## What is in the code?

| File | Responsibility |
| --- | --- |
| `kalshi_record.py` | Poll public REST quotes for all open temperature ranges in selected supported series and new event dates; save JSONL and basket scans. |
| `kalshi_paper.py` | Validate quotes, replay mean reversion, train and explain a regression filter. |
| `kalshi_arbitrage.py` | Validate complete outcome coverage for LA, New York and Chicago and calculate basket cost. |
| `kalshi_dashboard.py` | Show quote activity, model decisions, and simulated performance. |
| `kalshi_live.py` | Receive public trades over an authenticated WebSocket and store them in SQLite. |
| `kalshi_replay.py` | Replay synthetic trades through the same validation and SQL aggregation. |

Read [the walkthrough](WALKTHROUGH.md) to follow one quote through the code.

```text
REST quotes → JSONL → strategy replay + regression → Streamlit
WebSocket trades → validation → SQLite → SQL minute summary
```

## Baseline strategies

**Mean reversion:** compare the midpoint with the previous five quotes. A drop of at least 4 cents signals a possible one-contract Yes purchase. Replay enters at the next ask within 30 seconds, then exits at the bid after reversion or a 20-second holding period when displayed size permits. Positions without an exit remain open.

**Regression filter:** predict net dollars from price drop and spread using earlier closed simulated trades. Accept positive predictions and show each input's contribution. The model filters the rule's existing attempts; it does not simulate a separate position schedule.

**Basket scan:** verify that an LA event's mutually exclusive integer temperature ranges cover both tails without gaps. Calculate $1 minus all Yes asks and assumed fees, requiring displayed size for every leg. A positive result is conditional on all legs filling, not a completed arbitrage trade.

Fees default to an assumed 1 cent per fill or basket leg. Predictions assume fills. Realized P&L and drawdown count closed simulated trades and exclude open-position losses. Five closed training trades enable the demo model but provide little evidence of an edge.

## Collect and evaluate

Public quote collection requires no credentials:

```sh
python kalshi_record.py --series KXHIGHLAX KXHIGHNY KXHIGHCHI --hours 72
python kalshi_record.py --scan-once
python kalshi_paper.py --sessions recordings --diagnostics
python kalshi_paper.py --sessions recordings --tune --summary-only
```

Collection polls every five seconds, discovers new event dates, and stops at the configured duration or storage threshold (`--max-mib`, default 150 MiB, checked between batches). Quotes and basket scans remain in `recordings/` and are ignored by Git.

Evaluation groups ranges by event date. It trains on earlier events and uses only later quotes newer than the last training quote. Optional tuning compares 2, 4, and 6-cent drops on training data. For a single file, the first 60% is training data:

```sh
python kalshi_paper.py --quotes data/demo_quotes.jsonl --summary-only
```

## Streaming trades and SQL

Run the offline SQLite demo:

```sh
python kalshi_replay.py --db ":memory:"
```

It demonstrates duplicate handling and aggregation by trade time, including out-of-order arrivals.

For live WebSocket trades, supply a Kalshi key ID and matching private key through environment variables. Keep the key file outside the repository:

```powershell
$env:KALSHI_API_KEY_ID = '<your key ID>'
$env:KALSHI_PRIVATE_KEY_PATH = 'C:\path\to\your\kalshi.key'
python kalshi_live.py --series KXHIGHLAX
```

Use `--demo` for sandbox credentials. The WebSocket collector discovers markets at startup; restart to pick up new ones. Reconnection can leave gaps. The REST quote collector discovers new markets continuously.

## Tests and next steps

```sh
python -m unittest -v
```

Tests use simulated messages and require no credentials. Saved experiments now provide fixed windows, open-exposure reporting, and fee/delay comparisons. Local databases, recordings, credentials, and virtual environments remain excluded from Git; bundled data is synthetic.

## Fixed experiments while collection continues

Use **Saved experiments** on the dashboard to choose training and test end timestamps and save a report. Each report stores its own quote/scan snapshot, settings, input checksum, code checksums and model dependency versions under ignored `experiments/`. It remains unchanged when collection grows. Each snapshot consumes additional local disk space outside the collector's recording limit.

```sh
python kalshi_experiment.py --demo
python kalshi_experiment.py --train-end 2026-11-11T16:00:00+00:00 --test-end 2026-11-11T17:00:00+00:00
python kalshi_experiment.py --reproduce experiments/REPLACE_WITH_SAVED_FOLDER
```

Reproduction verifies unchanged inputs, code and dependency versions and checks exact result equality. Existing reports remain readable after code changes; reproducing an older report requires its original code and dependencies.

For real recordings, the earliest two-thirds of available event dates train the models; all cities on a date stay together. Training quotes stop at the chosen cutoff. Later event dates supply test quotes strictly after that cutoff and through the fixed end. The synthetic example splits one constructed stream by time. The initial 60% time cutoff is just a starting value, not an optimized setting.

Reports compare a fixed 4-cent rule, regression filter, extended regression, and no trades across flat fees of 0/1/2 cents per fill and delays of 0/5/15 seconds. Entry always needs a later quote, even at zero delay. Delayed exits use a quote after the exit trigger plus the delay. Missing entry quotes after 30 seconds are unfilled; unavailable exits remain open. Displayed quotes do not guarantee execution.

Additional model inputs are recent midpoint momentum and bid/ask size imbalance, both known at signal time. Extended regression requires 30 closed training trades. Reports flag fewer than 30 training trades, 30 closed test trades or three tested events as insufficient evidence; meeting these counts is not proof of profitability. Model comparisons are exploratory: selecting a variant from these results requires a fresh test window.

Open exposure includes entry cost and an indicative liquidation P&L after assumed exit fees. Stale marks and insufficient exit size are counted. Closed-trade drawdown still excludes open positions. Market diagnostics show spread, movement and gaps over 20 seconds. Basket reports count candidate observations, episodes, observed duration, and later snapshots retaining positive edge and all-leg size. Gaps over 20 seconds break episodes; repeated observations are not added up as profits.

The collector writes `recordings/health.json` with each series' last success, errors, storage usage and deadline. The dashboard marks an old heartbeat as stale. A failed city request does not block others. All series share the recording limit, checked before every row write.

Fee scenarios are transparent sensitivity assumptions, not a current fee calculator. The exchange's actual fees vary with price and contract rules: [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf). Event discovery uses the [public events API](https://docs.kalshi.com/api-reference/events/get-events). Only verified integer temperature baskets for the three supported series are scanned; other series fail the coverage check.
