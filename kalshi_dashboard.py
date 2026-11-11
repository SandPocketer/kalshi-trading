"""Read-only dashboard for the Kalshi paper-trading replay."""

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from kalshi_paper import (
    MIN_DROP, Attempt, Quote, fit_model, performance_summary, read_quotes,
    replay, score_signals, select_threshold, session_diagnostics, split_sessions,
)


RECORDINGS = Path(__file__).with_name("recordings")


def utc_time(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp / 1000, timezone.utc)


def metrics(attempts: list[Attempt], fee: float) -> dict:
    stats = performance_summary(attempts, fee)
    closed = stats["closed"]
    return {
        "Signals": stats["signals"],
        "Closed": closed,
        "Win rate": f"{stats['wins'] / closed:.0%}" if closed else "—",
        "Unfilled": stats["unfilled"],
        "Open": stats["open"],
        "Net P&L": stats["net"],
        "Max drawdown": stats["drawdown"],
    }


def quote_panel(sessions: list[tuple[str, list[Quote]]], size: int) -> None:
    if not sessions:
        st.info("Waiting for quote recordings.")
        return
    markets = {market: quotes for market, quotes in sessions}
    market = st.selectbox("Market", list(markets), index=len(markets) - 1)
    quotes = markets[market]
    stats = session_diagnostics(quotes)
    columns = st.columns(4)
    columns[0].metric("Quote snapshots", f"{stats['snapshots']:,}")
    columns[1].metric("Midpoint changes", f"{stats['midpoint_changes']:,}")
    columns[2].metric("Median spread", f"{stats['median_spread'] * 100:.2f}¢")
    columns[3].metric("Recording size", f"{size / (1024 ** 2):.2f} MiB")
    st.caption(f"Latest snapshot {utc_time(quotes[-1].timestamp):%Y-%m-%d %H:%M:%S UTC}. "
               "Chart shows up to 1,200 recent snapshots.")
    chart = pd.DataFrame([
        {"Time (UTC)": utc_time(q.timestamp), "Bid": q.bid * 100,
         "Ask": q.ask * 100, "Midpoint": q.midpoint * 100}
        for q in quotes[-1200:]
    ])
    st.line_chart(chart, x="Time (UTC)", y=["Bid", "Midpoint", "Ask"], y_label="Price (¢)")


def performance(training: list[Attempt], held_out: list[Attempt], fee: float) -> None:
    model, training_count = fit_model(training, fee)
    scored = score_signals(model, held_out)
    selected = [(attempt, score) for attempt, score, take in scored if take]
    taken = [a for a, _ in selected]
    expected = sum(score for _, score in selected)
    rule = metrics(held_out, fee)
    filtered = metrics(taken, fee)
    st.caption(f"Fit on {training_count} earlier closed trades. "
               f"Scored {len(held_out)} held-out signals at ${fee:.2f} per fill.")
    columns = st.columns(4)
    columns[0].metric("Predicted net if filled", f"${expected:+.2f}" if held_out else "—")
    columns[1].metric("Realized model P&L", f"${filtered['Net P&L']:+.2f}")
    columns[2].metric("Model win rate", filtered["Win rate"])
    columns[3].metric("Closed-trade drawdown", f"${filtered['Max drawdown']:.2f}")
    st.caption("Expected P&L sums the model’s predictions for accepted signals, assuming fills. "
               "Realized P&L and drawdown use closed simulated trades. Open-position losses are excluded. "
               "This model filters rule-generated signals; it does not replay its own position schedule.")
    table = pd.DataFrame([
        {"Strategy": "Rule only", **rule},
        {"Strategy": "Rule + model", **filtered},
        {"Strategy": "No trades", **metrics([], fee)},
    ]).set_index("Strategy")
    st.dataframe(table, width="stretch", column_config={
        "Net P&L": st.column_config.NumberColumn(format="$%.2f"),
        "Max drawdown": st.column_config.NumberColumn(format="$%.2f"),
    })
    if held_out:
        with st.expander("Signal decisions"):
            st.dataframe([
                {"Time (UTC)": utc_time(attempt.signal.timestamp),
                 "Decision": "Take" if take else "Skip",
                 "Expected net (¢)": round(score * 100, 2),
                 "Drop contribution (¢)": round(model.coef_[0] * attempt.features[0] * 100, 2),
                 "Spread contribution (¢)": round(model.coef_[1] * attempt.features[1] * 100, 2),
                 "Rule outcome": attempt.reason, "Realized net ($)": attempt.net(fee)}
                for attempt, score, take in scored
            ], width="stretch")
            st.caption(f"Model intercept {model.intercept_ * 100:+.2f}¢ per signal.")


def arbitrage_panel(path: Path) -> None:
    st.subheader("Complete-event paper arbitrage")
    try:
        with path.open("rb") as source:
            offset = max(0, source.seek(0, 2) - 32_768)
            source.seek(offset)
            if offset:
                source.readline()
            lines = source.read().split(b"\n")[:-1]
        scans = [json.loads(line) for line in lines if line]
    except (OSError, ValueError):
        scans = []
    if not scans:
        st.info("Waiting for a basket scan.")
        return
    latest = max(scan["ts_ms"] for scan in scans)
    rows = [
        {"Event": scan["event_ticker"], "Status": scan["status"],
         "Ask cost ($)": float(scan["ask_cost"]) if "ask_cost" in scan else None,
         "Assumed fees ($)": float(scan["assumed_fees"]) if "assumed_fees" in scan else None,
         "Net if filled ($)": float(scan["net_if_filled"]) if "net_if_filled" in scan else None,
         "Smallest ask size": float(scan["minimum_ask_size"]) if "minimum_ask_size" in scan else None}
        for scan in scans if scan["ts_ms"] == latest
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(f"Snapshot {utc_time(latest):%Y-%m-%d %H:%M:%S UTC}. "
               "One Yes contract per range. Net assumes every leg fills at its displayed ask.")


@st.fragment(run_every="30s")
def dashboard(demo: bool, fee: float, tune: bool) -> None:
    if demo:
        sample = Path(__file__).with_name("data") / "demo_quotes.jsonl"
        market, quotes = read_quotes(sample, None)
        sessions = [(market, quotes)]
        size = sample.stat().st_size
        st.info("Synthetic educational sample. These prices were constructed to demonstrate "
                "the pipeline and model; results are not evidence of profitability.")
    else:
        paths = sorted(RECORDINGS.glob("*.quotes.jsonl"))
        basket_path = RECORDINGS / "arbitrage.jsonl"
        size = sum(path.stat().st_size for path in paths)
        if basket_path.exists():
            size += basket_path.stat().st_size
        sessions = []
        for path in paths:
            try:
                sessions.append(read_quotes(path, None, live=True))
            except (OSError, ValueError) as exc:
                st.warning(f"Could not read {path.name}. {exc}")
        sessions.sort(key=lambda session: session[1][0].timestamp)

    st.subheader("Market activity")
    quote_panel(sessions, size)
    if not demo:
        arbitrage_panel(basket_path)
    st.subheader("Paper performance")
    try:
        if demo:
            cutoff = len(quotes) * 3 // 5
            training, held_out = replay(quotes[:cutoff]), replay(quotes[cutoff:])
        else:
            earlier, later = split_sessions(sessions)
            threshold = select_threshold(earlier, fee, verbose=False) if tune else MIN_DROP
            st.caption(f"Evaluation threshold {threshold * 100:g}¢. "
                       f"{len(earlier)} training sessions, {len(later)} held-out sessions.")
            training = [a for _, quotes in earlier for a in replay(quotes, min_drop=threshold)]
            held_out = [a for _, quotes in later for a in replay(quotes, min_drop=threshold)]
        performance(training, held_out, fee)
    except ValueError as exc:
        st.metric("Predicted net if filled", "—")
        st.info(f"Model results pending. {str(exc).capitalize()}.")
        if sessions:
            attempts = [attempt for _, quotes in sessions for attempt in replay(quotes)]
            summary = metrics(attempts, fee)
            st.caption("Rule-only replay across all available quotes at the default 4¢ threshold.")
            columns = st.columns(3)
            columns[0].metric("Rule signals", summary["Signals"])
            columns[1].metric("Rule closed trades", summary["Closed"])
            columns[2].metric("Rule realized P&L", f"${summary['Net P&L']:+.2f}")


st.set_page_config(page_title="Kalshi Paper Trading", layout="wide")
st.title("Kalshi Paper Trading")
st.caption("Quote monitoring and simulated performance · Refreshes every 30 seconds")
source = st.sidebar.radio("Data", ["Recordings", "Synthetic demo"],
                          index=0 if any(RECORDINGS.glob("*.quotes.jsonl")) else 1)
with st.expander("How this demo works"):
    st.markdown("**Quotes → replay → model → results**\n\n"
                "A quote shows the current buying and selling prices. The rule looks for a "
                "drop below the previous five midpoints. Replay simulates buying at the next "
                "ask and selling at a later bid. Linear regression learns from earlier "
                "simulated trades, then scores later signals. Expand Signal decisions "
                "below to see why each signal was accepted or skipped.")
fee = st.sidebar.number_input("Fee per fill ($)", min_value=0.0, value=0.01,
                              step=0.005, format="%.3f")
tune = st.sidebar.checkbox("Tune threshold on earlier sessions", value=True,
                           disabled=source == "Synthetic demo")
dashboard(source == "Synthetic demo", fee, tune)
