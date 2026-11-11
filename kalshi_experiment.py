"""Freeze local inputs and compare paper strategies on fixed time windows."""

import argparse
from bisect import bisect_left
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
from statistics import median
import uuid

from sklearn.linear_model import LinearRegression
from kalshi_paper import Quote, performance_summary, read_quotes, replay

MIN_EVIDENCE = 30


def event_key(ticker):
    match = re.match(r'(KX[A-Z0-9]+-\d{2}[A-Z]{3}\d{2})-', ticker)
    return match.group(1) if match else ticker


def fixed_split(sessions, cutoff, end, synthetic=False):
    """Keep related event dates together, and exclude overlapping training time."""
    days = sorted({event_key(t).split('-')[-1] for t, _ in sessions},
                  key=lambda d: datetime.strptime(d, '%y%b%d') if re.fullmatch(r'\d{2}[A-Z]{3}\d{2}', d) else datetime.min)
    training_days = set(days[:max(1, len(days) * 2 // 3)])
    train, test = [], []
    for ticker, quotes in sessions:
        training_event = event_key(ticker).split('-')[-1] in training_days
        before = [q for q in quotes if q.timestamp <= cutoff]
        after = [q for q in quotes if cutoff < q.timestamp <= end]
        if training_event and before:
            train.append((ticker, before))
        if (synthetic or not training_event) and after:
            test.append((ticker, after))
    return train, test


def basket_history(scans, fee, delay_ms, max_gap_ms=20_000):
    """Summarize candidate observations, never sum overlapping hypothetical profits."""
    groups = {}
    for scan in scans:
        groups.setdefault(scan['event_ticker'], []).append(scan)
    results = []
    def edge(row):
        if 'legs' not in row or float(row.get('minimum_ask_size', 0)) < 1:
            return None
        return 1 - sum(float(p) for p in row['legs'].values()) - fee * len(row['legs'])
    for event, rows in sorted(groups.items()):
        rows.sort(key=lambda r: r['ts_ms'])
        times = [r['ts_ms'] for r in rows]
        candidates = episodes = checked = survived = pending = 0
        durations = []
        start = previous = None
        for i, row in enumerate(rows):
            value = edge(row)
            candidate = value is not None and value > 1e-9
            if start is not None and (not candidate or row['ts_ms'] - previous > max_gap_ms):
                durations.append((previous - start) / 1000)
                start = None
            if candidate:
                candidates += 1
                if start is None:
                    episodes += 1
                    start = row['ts_ms']
                # Even zero delay requires a later independent snapshot.
                next_index = bisect_left(times, max(row['ts_ms'] + 1, row['ts_ms'] + delay_ms), lo=i + 1)
                following = rows[next_index] if next_index < len(rows) else None
                if following is None or following['ts_ms'] - row['ts_ms'] > max_gap_ms:
                    pending += 1
                else:
                    checked += 1
                    later_edge = edge(following)
                    if set(following.get('legs', {})) == set(row['legs']) and later_edge is not None and later_edge > 1e-9:
                        survived += 1
            previous = row['ts_ms']
        if start is not None:
            durations.append((previous - start) / 1000)
        results.append({'event': event, 'scans': len(rows), 'candidate_snapshots': candidates,
                        'episodes': episodes, 'longest_observed_seconds': max(durations, default=0),
                        'checked_later': checked, 'positive_later_with_size': survived,
                        'unconfirmed': pending})
    return results


def exposure(attempts, quotes, fee, end):
    opened = [a for a in attempts if a.entry is not None and a.exit is None]
    last = quotes[-1]
    return {'open_cost': sum(a.entry.ask + fee for a in opened),
            'open_liquidation_pnl': sum(last.bid - a.entry.ask - 2 * fee for a in opened),
            'open_without_exit_size': len(opened) if last.bid_size < 1 else 0,
            'stale_open_marks': len(opened) if end - last.timestamp > 30_000 else 0}


def feature_values(attempt, extended):
    return (*attempt.features, attempt.momentum, attempt.imbalance) if extended else attempt.features


def evaluate(sessions, scans, settings):
    train, test = fixed_split(sessions, settings['train_end_ms'], settings['test_end_ms'], settings['synthetic'])
    rows, baskets = [], []
    for delay in settings['delays_ms']:
        training = [a for _, qs in train for a in replay(qs, delay_ms=delay)]
        closed_train = [a for a in training if a.exit is not None]
        attempted = [(ticker, qs, replay(qs, delay_ms=delay)) for ticker, qs in test]
        for fee in settings['fees']:
            models = {}
            for name, extended in [('Regression', False), ('Regression + price features', True)]:
                required = MIN_EVIDENCE if extended else 5
                if len(closed_train) >= required:
                    models[name] = LinearRegression().fit(
                        [feature_values(a, extended) for a in closed_train], [a.net(fee) for a in closed_train])
            for strategy in ('Rule', 'Regression', 'Regression + price features', 'No trades'):
                all_attempts = []
                totals = dict(open_cost=0., open_liquidation_pnl=0., open_without_exit_size=0, stale_open_marks=0)
                event_rows = {}
                prediction = 0.
                available = strategy in ('Rule', 'No trades') or strategy in models
                for ticker, qs, attempts in attempted:
                    selected = attempts if strategy == 'Rule' else []
                    if strategy in models and attempts:
                        extended = strategy.endswith('price features')
                        scores = models[strategy].predict([feature_values(a, extended) for a in attempts])
                        selected = [a for a, score in zip(attempts, scores) if score > 0]
                        prediction += sum(float(score) for score in scores if score > 0)
                    all_attempts.extend(selected)
                    exp = exposure(selected, qs, fee, settings['test_end_ms'])
                    for key, value in exp.items():
                        totals[key] += value
                    event = event_rows.setdefault(event_key(ticker), {'attempts': [], 'open_liquidation_pnl': 0.})
                    event['attempts'].extend(selected)
                    event['open_liquidation_pnl'] += exp['open_liquidation_pnl']
                stats = performance_summary(all_attempts, fee)
                enough = len(closed_train) >= MIN_EVIDENCE and stats['closed'] >= MIN_EVIDENCE and len(event_rows) >= 3
                status = 'descriptive results; not proof of an edge' if enough else 'insufficient evidence'
                if not available:
                    status = 'model pending: insufficient closed training trades'
                rows.append({'strategy': strategy, 'fee': fee, 'delay_seconds': delay / 1000,
                             'status': status, 'training_closed': len(closed_train),
                             **stats, **totals, 'predicted_if_filled': prediction if strategy in models else None,
                             'net_plus_open_mark': stats['net'] + totals['open_liquidation_pnl'],
                             'events': [{'event': key, **performance_summary(value['attempts'], fee),
                                         'open_liquidation_pnl': value['open_liquidation_pnl']}
                                        for key, value in sorted(event_rows.items())]})
            baskets.extend({'fee': fee, 'delay_seconds': delay / 1000, **r}
                           for r in basket_history(scans, fee, delay))
    diagnostics = []
    for ticker, qs in sessions:
        gaps = [b.timestamp - a.timestamp for a, b in zip(qs, qs[1:])]
        diagnostics.append({'market': ticker, 'snapshots': len(qs), 'gaps_over_20s': sum(g > 20_000 for g in gaps),
                            'largest_gap_seconds': max(gaps, default=0) / 1000,
                            'median_spread': median(q.ask - q.bid for q in qs),
                            'midpoint_changes': sum(a.midpoint != b.midpoint for a, b in zip(qs, qs[1:]))})
    return {'settings': settings, 'training_markets': [t for t, _ in train],
            'test_markets': [t for t, _ in test], 'comparison': rows, 'baskets': baskets,
            'diagnostics': diagnostics,
            'notes': ['Fees are flat sensitivity assumptions, not an exchange fee calculator.',
                      'Zero delay uses the next available entry quote; exits use the trigger quote.',
                      'Open marks subtract hypothetical exit fees and may not be executable.',
                      'Drawdown uses closed trades only. Model variants filter the rule schedule.',
                      'Basket persistence is observational, with a 20-second maximum confirmation gap.',
                      'Feature comparison is exploratory. Selecting a winner requires a new held-out window.']}


def freeze(sessions, basket_path, destination, cutoff, end, synthetic=False):
    if not cutoff < end:
        raise ValueError('Training end must precede test end.')
    if not sessions or end > max(q.timestamp for _, qs in sessions for q in qs):
        raise ValueError('Test end must be within collected data.')
    frozen = [(t, [q for q in qs if q.timestamp <= end]) for t, qs in sessions]
    frozen = [(t, qs) for t, qs in frozen if qs]
    if not frozen or cutoff < min(q.timestamp for _, qs in frozen for q in qs):
        raise ValueError('Training window has no data.')
    scans = []
    if basket_path and basket_path.exists():
        with basket_path.open(encoding='utf-8') as source:
            for line in source:
                if not line.endswith('\n'):
                    break
                row = json.loads(line)
                if cutoff < row['ts_ms'] <= end:
                    scans.append(row)
    settings = {'train_end_ms': cutoff, 'test_end_ms': end, 'synthetic': synthetic,
                'fees': [0., .01, .02], 'delays_ms': [0, 5000, 15000], 'threshold': .04,
                'window': 5, 'hold_ms': 20000, 'entry_wait_ms': 30000,
                'created_utc': datetime.now(timezone.utc).isoformat()}
    payload = {'sessions': [(t, [asdict(q) for q in qs]) for t, qs in frozen], 'scans': scans}
    raw = json.dumps(payload, separators=(',', ':')).encode()
    settings['dependencies'] = {name: version(name) for name in ('scikit-learn', 'numpy')}
    settings['input_sha256'] = hashlib.sha256(raw).hexdigest()
    settings['code_sha256'] = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                               for name in ('kalshi_experiment.py', 'kalshi_paper.py')}
    report = evaluate(frozen, scans, settings)
    folder = destination / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    (folder / 'inputs.json').write_bytes(raw)
    (folder / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return folder


def reproduce(folder):
    saved = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    settings = saved['settings']
    raw = (folder / 'inputs.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != settings['input_sha256']:
        raise ValueError('Frozen input checksum changed')
    for name, expected in settings['code_sha256'].items():
        if hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != expected:
            raise ValueError('Code changed since this report: ' + name)
    for name, expected in settings['dependencies'].items():
        if version(name) != expected:
            raise ValueError('Dependency version changed: ' + name)
    payload = json.loads(raw)
    sessions = [(t, [Quote(**q) for q in qs]) for t, qs in payload['sessions']]
    result = evaluate(sessions, payload['scans'], settings)
    if result != saved:
        raise ValueError('Replayed results differ from saved report')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=Path, default=Path('recordings'))
    parser.add_argument('--reproduce', type=Path, help='verify and rerun a saved experiment directory')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--train-end', help='ISO timestamp with timezone')
    parser.add_argument('--test-end', help='ISO timestamp with timezone')
    parser.add_argument('--output', type=Path, default=Path('experiments'))
    args = parser.parse_args()
    if args.reproduce:
        try:
            reproduce(args.reproduce)
            print('Reproduced saved report exactly.')
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        return
    paths = [Path(__file__).with_name('data') / 'demo_quotes.jsonl'] if args.demo else sorted(args.sessions.glob('*.quotes.jsonl'))
    sessions = [read_quotes(p, None, live=True) for p in paths]
    if not sessions:
        parser.error('No recordings available')
    low = min(qs[0].timestamp for _, qs in sessions)
    high = max(qs[-1].timestamp for _, qs in sessions)
    def timestamp(value, default):
        if not value:
            return default
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError('Include a timezone in timestamps')
        return int(dt.timestamp() * 1000)
    try:
        end = timestamp(args.test_end, high)
        cutoff = timestamp(args.train_end, low + (end - low) * 3 // 5)
        print(freeze(sessions, None if args.demo else args.sessions / 'arbitrage.jsonl', args.output, cutoff, end, args.demo))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
