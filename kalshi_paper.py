"""Replay a one-contract mean-reversion strategy and explain an ML filter."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re
from statistics import mean, median

from sklearn.linear_model import LinearRegression


WINDOW = 5
MIN_DROP = 0.04
MIN_TRAIN_TRADES = 5
ENTRY_WAIT_MS = 30_000
HOLD_MS = 20_000
FEATURES = ("price drop", "spread")


@dataclass(frozen=True)
class Quote:
    timestamp: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Attempt:
    signal: Quote
    average: float
    reason: str
    entry: Quote | None = None
    exit: Quote | None = None

    @property
    def features(self) -> tuple[float, float]:
        return self.average - self.signal.midpoint, self.signal.ask - self.signal.bid

    def net(self, fee: float) -> float | None:
        if self.entry is None or self.exit is None:
            return None
        return self.exit.bid - self.entry.ask - 2 * fee


def read_quotes(path: Path, market: str | None, *, live: bool = False) -> tuple[str, list[Quote]]:
    quotes = []
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, start=1):
            # A running writer may not have finished the final JSONL record yet.
            if live and not line.endswith("\n"):
                break
            try:
                data = json.loads(line)
                ticker = data["market_ticker"]
                if market is None:
                    market = ticker
                elif ticker != market:
                    continue
                quote = Quote(data["ts_ms"], float(data["yes_bid_dollars"]),
                              float(data["yes_ask_dollars"]), float(data["yes_bid_size_fp"]),
                              float(data["yes_ask_size_fp"]))
                if (type(quote.timestamp) is not int or not 0 <= quote.timestamp <= 253402300799000
                        or not 0 <= quote.bid <= quote.ask <= 1
                        or not all(isfinite(size) and size >= 0
                                   for size in (quote.bid_size, quote.ask_size))):
                    raise ValueError("invalid timestamp, price, or size")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"quote file line {number}: {exc}") from exc
            if not quotes or quote.timestamp >= quotes[-1].timestamp:
                quotes.append(quote)
    if not quotes:
        raise ValueError("no quotes found for the selected market")
    return market, quotes


def replay(quotes: list[Quote], *, min_drop: float = MIN_DROP) -> list[Attempt]:
    attempts = []
    next_signal = WINDOW
    for index in range(WINDOW, len(quotes)):
        if index < next_signal:
            continue
        signal = quotes[index]
        history = quotes[index - WINDOW:index]
        average = mean(quote.midpoint for quote in history)
        if signal.timestamp - history[0].timestamp > 60_000 or round(average - signal.midpoint, 10) < min_drop:
            continue

        next_signal = index + 2
        if index + 1 == len(quotes):
            attempts.append(Attempt(signal, average, "no later quote"))
            break
        entry = quotes[index + 1]
        if entry.timestamp - signal.timestamp > ENTRY_WAIT_MS or entry.ask_size < 1:
            attempts.append(Attempt(signal, average, "next ask was late or lacked size"))
            continue

        for exit_index in range(index + 2, len(quotes)):
            quote = quotes[exit_index]
            reverted = quote.midpoint >= average
            expired = quote.timestamp - entry.timestamp >= HOLD_MS
            if quote.bid_size >= 1 and (reverted or expired):
                reason = "mean reached" if reverted else "holding period elapsed"
                attempts.append(Attempt(signal, average, reason, entry, quote))
                next_signal = exit_index + 1
                break
        else:
            attempts.append(Attempt(signal, average, "position open at end of replay", entry))
            break
    return attempts


def session_diagnostics(quotes: list[Quote]) -> dict:
    midpoints = [round(quote.midpoint, 6) for quote in quotes]
    attempts = replay(quotes)
    return {
        "snapshots": len(quotes),
        "minutes": (quotes[-1].timestamp - quotes[0].timestamp) / 60_000,
        "midpoint_low": min(midpoints),
        "midpoint_high": max(midpoints),
        "midpoint_changes": sum(a != b for a, b in zip(midpoints, midpoints[1:])),
        "median_spread": median(quote.ask - quote.bid for quote in quotes),
        "signals": len(attempts),
        "closed": sum(attempt.exit is not None for attempt in attempts),
    }


def print_diagnostics(market: str, quotes: list[Quote]) -> None:
    stats = session_diagnostics(quotes)
    print(market)
    print(f"  {stats['snapshots']} snapshots over {stats['minutes']:.1f} min")
    print(f"  Midpoint {stats['midpoint_low'] * 100:.2f} to {stats['midpoint_high'] * 100:.2f}c"
          f"  |  {stats['midpoint_changes']} changes")
    print(f"  Median spread {stats['median_spread'] * 100:.2f}c"
          f"  |  signals={stats['signals']} at {MIN_DROP * 100:g}c"
          f"  |  closed trades={stats['closed']}")


def closed_results(attempts: list[Attempt], fee: float) -> list[float]:
    closed = sorted((attempt for attempt in attempts if attempt.exit is not None),
                    key=lambda attempt: attempt.exit.timestamp)
    return [attempt.net(fee) for attempt in closed]


def performance_summary(attempts: list[Attempt], fee: float) -> dict:
    results = closed_results(attempts, fee)
    balance = peak = drawdown = 0.0
    for net in results:
        balance += net
        peak = max(peak, balance)
        drawdown = max(drawdown, peak - balance)
    return {
        "signals": len(attempts),
        "closed": len(results),
        "wins": sum(net > 1e-9 for net in results),
        "unfilled": sum(attempt.entry is None for attempt in attempts),
        "open": sum(attempt.entry is not None and attempt.exit is None for attempt in attempts),
        "net": balance,
        "drawdown": drawdown,
    }


def summarize(label: str, attempts: list[Attempt], fee: float) -> None:
    stats = performance_summary(attempts, fee)
    print(f"{label:12} signals={stats['signals']}  closed={stats['closed']}  wins={stats['wins']}  "
          f"unfilled={stats['unfilled']}  open={stats['open']}  realized P&L=${stats['net']:+.2f}")


def report(quotes: list[Quote], fee: float, *, summary_only: bool = False) -> None:
    cutoff = len(quotes) * 3 // 5
    compare(replay(quotes[:cutoff]), replay(quotes[cutoff:]), fee, summary_only=summary_only)


def select_threshold(training_sessions: list[tuple[str, list[Quote]]], fee: float, *,
                     verbose: bool = True) -> float:
    if verbose:
        print(f"Threshold comparison on training sessions (fee ${fee:.2f} per fill)")
    ranked = []
    for threshold in (0.02, MIN_DROP, 0.06):
        attempts = [attempt for _, quotes in training_sessions
                    for attempt in replay(quotes, min_drop=threshold)]
        if verbose:
            summarize(f"{threshold * 100:g}c drop", attempts, fee)
        results = closed_results(attempts, fee)
        if len(results) >= MIN_TRAIN_TRADES:
            ranked.append((round(sum(results), 10), -abs(threshold - MIN_DROP), threshold))

    if not ranked:
        raise ValueError(f"need at least {MIN_TRAIN_TRADES} closed training trades at a threshold")
    _, _, selected = max(ranked)
    if verbose:
        print(f"Selected {selected * 100:g}c using training P&L. Now evaluating later sessions.\n")
    return selected


def split_sessions(sessions: list[tuple[str, list[Quote]]]):
    # Range tickers on the same Kalshi weather date share an outcome.
    events = {}
    for market, quotes in sessions:
        match = re.fullmatch(r"(KX[A-Z0-9]+-(\d{2}[A-Z]{3}\d{2}))-.+", market)
        key = match.group(1) if match else market
        events.setdefault(key, []).append((market, quotes))
    if len(events) < 2:
        raise ValueError("need at least two recorded events for a chronological split")

    def event_order(item):
        key, markets = item
        start = min(quotes[0].timestamp for _, quotes in markets)
        if re.fullmatch(r"KX[A-Z0-9]+-\d{2}[A-Z]{3}\d{2}", key):
            day = datetime.strptime(key.rsplit("-", 1)[1], "%y%b%d").replace(tzinfo=timezone.utc)
            return day.timestamp() * 1000, start
        return start, start

    ordered = [markets for _, markets in sorted(events.items(), key=event_order)]
    cutoff = len(ordered) * 2 // 3
    earlier = [session for event in ordered[:cutoff] for session in event]
    last_training_quote = max(quotes[-1].timestamp for _, quotes in earlier)
    later = []
    for event in ordered[cutoff:]:
        for market, quotes in event:
            after_training = [quote for quote in quotes if quote.timestamp > last_training_quote]
            if after_training:
                later.append((market, after_training))
    if not later:
        raise ValueError("waiting for held-out quotes newer than the training data")
    return earlier, later


def report_sessions(directory: Path, fee: float, *, summary_only: bool = False,
                    tune: bool = False) -> None:
    sessions = [read_quotes(path, None) for path in directory.glob("*.quotes.jsonl")]
    earlier, later = split_sessions(sessions)
    sessions = earlier + later
    cutoff = len(earlier)
    threshold = select_threshold(earlier, fee) if tune else MIN_DROP
    training = []
    held_out = []
    for index, (market, quotes) in enumerate(sessions):
        attempts = replay(quotes, min_drop=threshold)
        is_training = index < cutoff
        label = "Train" if is_training else "Test"
        closed = sum(attempt.exit is not None for attempt in attempts)
        print(f"{label:5} {market:25} quotes={len(quotes)}  signals={len(attempts)}  closed={closed}")
        (training if is_training else held_out).extend(attempts)
    print()
    compare(training, held_out, fee, summary_only=summary_only)
    if tune:
        summarize("No trades", [], fee)


def fit_model(training_attempts: list[Attempt], fee: float) -> tuple[LinearRegression, int]:
    training = [(attempt.features, net) for attempt in training_attempts
                if (net := attempt.net(fee)) is not None]
    if len(training) < MIN_TRAIN_TRADES:
        raise ValueError(f"need at least {MIN_TRAIN_TRADES} closed training trades; found {len(training)}")
    features, targets = zip(*training)
    model = LinearRegression().fit(features, targets)
    return model, len(training)


def score_signals(model: LinearRegression, attempts: list[Attempt]) -> list[tuple[Attempt, float, bool]]:
    if not attempts:
        return []
    predictions = model.predict([attempt.features for attempt in attempts])
    return [(attempt, float(score), bool(score > 1e-9))
            for attempt, score in zip(attempts, predictions)]


def compare(training_attempts: list[Attempt], held_out: list[Attempt], fee: float, *,
            summary_only: bool) -> None:
    model, training_count = fit_model(training_attempts, fee)
    taken = []
    print(f"Trained on {training_count} closed trades. Held-out signals: {len(held_out)}.")
    print("Model predicts net dollars from the price drop and bid-ask spread.\n")
    for attempt, predicted, take in score_signals(model, held_out):
        if take:
            taken.append(attempt)
        if summary_only:
            continue
        timestamp = datetime.fromtimestamp(attempt.signal.timestamp / 1000, timezone.utc)
        contributions = ", ".join(
            f"{label} {value * 100:.1f}c ({coefficient * value * 100:+.2f}c)"
            for label, value, coefficient in zip(FEATURES, attempt.features, model.coef_)
        )
        decision = "TAKE" if take else "SKIP"
        net = attempt.net(fee)
        baseline = f"{attempt.reason}, net ${net:+.2f}" if net is not None else attempt.reason
        print(f"{timestamp:%Y-%m-%d %H:%M:%S} UTC  {decision}  predicted {predicted * 100:+.2f}c  "
              f"= intercept {model.intercept_ * 100:+.2f}c + {contributions}")
        print(f"  Rule outcome: {baseline}")

    print()
    summarize("Rule only", held_out, fee)
    summarize("Rule + model", taken, fee)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--quotes", type=Path, help="recorded Kalshi ticker JSONL")
    source.add_argument("--sessions", type=Path, help="directory of separate quote sessions")
    parser.add_argument("--market", help="market ticker to select from the recording")
    parser.add_argument("--fee", type=float, default=0.01, help="simulated dollars per fill")
    parser.add_argument("--summary-only", action="store_true", help="hide individual model decisions")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--diagnostics", action="store_true", help="show quote activity without fitting a model")
    mode.add_argument("--tune", action="store_true", help="select a threshold using earlier sessions")
    args = parser.parse_args()
    if not isfinite(args.fee) or args.fee < 0:
        parser.error("--fee must be finite and nonnegative")
    if args.market and not args.quotes:
        parser.error("--market requires --quotes")
    if args.tune and not args.sessions:
        parser.error("--tune requires --sessions")
    try:
        if args.sessions:
            if args.diagnostics:
                paths = sorted(args.sessions.glob("*.quotes.jsonl"))
                if not paths:
                    raise ValueError("no recorded sessions found")
                for path in paths:
                    print_diagnostics(*read_quotes(path, None))
            else:
                report_sessions(args.sessions, args.fee, summary_only=args.summary_only,
                                tune=args.tune)
            return
        if args.quotes:
            market, quotes = read_quotes(args.quotes, args.market)
            label = f"{market} ({len(quotes)} recorded quotes)"
        else:
            _, quotes = read_quotes(Path(__file__).with_name("data") / "demo_quotes.jsonl", None)
            label = "Synthetic quote demo"
        if args.diagnostics:
            print_diagnostics(label, quotes)
        else:
            print(label)
            report(quotes, args.fee, summary_only=args.summary_only)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
