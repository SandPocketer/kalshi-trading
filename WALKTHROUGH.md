# Follow one quote through the project

Read these functions in order. You do not need to understand every option or error handler first.

## 1. Get a price — kalshi_record.py

`open_events()` asks Kalshi for the open events in our series. Each event is a day, with several markets for different temperature ranges. `quote_snapshot()` keeps the ticker, time, bid, ask, and available size. `record()` repeats this and `append_row()` saves one JSON object per line.

A bid is what a buyer currently offers. An ask is what a seller currently requests. These are quotes, not completed trades. This collector polls REST every five seconds; `kalshi_live.py` is the separate WebSocket trade collector.

## 2. Load the prices — kalshi_paper.py

`read_quotes()` turns each saved line into a `Quote`. That small dataclass is just a named container: timestamp, bid, ask, and sizes. Its `midpoint` property averages bid and ask.

The public demo starts here with `data/demo_quotes.jsonl`, so visitors do not have to wait for a market to move.

## 3. Try the rule — replay()

Suppose the previous five midpoints average 50 cents and the current midpoint falls to 44 cents. The 6-cent drop exceeds our 4-cent threshold, creating a signal.

Replay looks at the next quote to simulate buying one Yes contract at the ask. It later sells at the bid when the midpoint returns to the earlier average or the holding period expires, provided there is enough displayed size. If no exit is available, the position remains open.

An `Attempt` remembers the signal, entry, exit, and reason. Buying at 45 cents and selling at 49 cents earns 4 cents before fees, or 2 cents after two assumed 1-cent fees. These are illustrative numbers, not a recorded trade.

## 4. Learn a filter — fit_model() and score_signals()

Linear regression learns a simple equation from earlier closed attempts:

```text
predicted net = intercept + weight × price drop + weight × spread
```

`score_signals()` applies that equation to later signals and accepts positive predictions. The dashboard shows the contribution of each input. This is the AI part: a small statistical model, without an LLM or external AI service.

The model filters the rule's existing attempts. It does not yet run its own independent trade schedule. A positive prediction does not guarantee a profit.

## 5. Display results — kalshi_dashboard.py

`quote_panel()` draws prices. `performance()` compares the plain rule, model filter, and no-trade baseline. Predictions and realized simulated P&L are different quantities. Drawdown currently counts closed trades only.

## 6. Check baskets — kalshi_arbitrage.py

`complete_la_event()` checks that the temperature outcomes cover the whole event without gaps or overlaps. `scan_basket()` adds the cost of buying one Yes contract for every outcome, then subtracts that cost and assumed fees from the combined $1 payout. The result assumes every leg fills; it is a candidate calculation, not an execution.

## Why the extra code exists

- Validation catches unusable prices before they affect a result.
- Chronological splitting keeps later information out of earlier training.
- Tests check specific mistakes, such as a missing basket outcome or reading a half-written line.
- The live reader waits for the writer to finish its last line.

These checks support the main flow. Start by understanding collection, replay, and display; return to the checks when you encounter the problem each one solves.

## Iterating on the baseline

The repository contains the educational strategies and the pipeline. Local recordings and databases stay on disk and are ignored by Git. Only synthetic samples are bundled for visitors.

Next research work: freeze evaluation periods, report open exposure and data gaps, and measure how results change with fees and delayed fills. Keep LA as the initial scope until those comparisons are reproducible.
