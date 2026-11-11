import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from decimal import Decimal

from kalshi_paper import Quote, read_quotes, replay
from kalshi_experiment import basket_history, evaluate, exposure, fixed_split, freeze, reproduce
from kalshi_record import record


def quotes(*times):
    return [Quote(t, .49, .51, 10, 10) for t in times]


class ExecutionTests(unittest.TestCase):
    def test_delay_applies_to_entry_and_exit(self):
        qs = quotes(0, 5000, 10000, 15000, 20000)
        qs += [Quote(t, .39, .41, 10, 10) for t in (25000, 30000, 35000)]
        qs += quotes(40000, 45000, 50000, 55000, 60000)
        immediate = replay(qs)[0]
        delayed = replay(qs, delay_ms=10000)[0]
        self.assertEqual(immediate.entry.timestamp, 30000)
        self.assertEqual(delayed.entry.timestamp, 35000)
        self.assertEqual(delayed.exit.timestamp, 50000)

    def test_delay_without_later_quote_is_unfilled(self):
        qs = quotes(0, 5000, 10000, 15000, 20000)
        qs += [Quote(25000, .39, .41, 10, 10), Quote(30000, .39, .41, 10, 10)]
        attempt = replay(qs, delay_ms=15000)[0]
        self.assertIsNone(attempt.entry)

    def test_open_exposure_reports_stale_unfillable_mark(self):
        qs = quotes(0, 5000, 10000, 15000, 20000)
        qs += [Quote(25000, .39, .41, 0, 10), Quote(30000, .39, .41, 0, 10)]
        result = exposure(replay(qs), qs, .01, 100000)
        self.assertAlmostEqual(result['open_cost'], .42)
        self.assertAlmostEqual(result['open_liquidation_pnl'], -.04)
        self.assertEqual(result['open_without_exit_size'], 1)
        self.assertEqual(result['stale_open_marks'], 1)


class ExperimentTests(unittest.TestCase):
    def test_same_date_locations_never_cross_event_split(self):
        sessions = [('KXHIGHLAX-26NOV11-B80', quotes(0, 100, 300)),
                    ('KXHIGHNY-26NOV11-B60', quotes(0, 100, 300)),
                    ('KXHIGHLAX-26NOV12-B80', quotes(100, 200, 300))]
        train, test = fixed_split(sessions, 150, 300)
        self.assertEqual(len(train), 2)
        self.assertEqual(len(test), 1)
        self.assertTrue(all(q.timestamp <= 150 for _, qs in train for q in qs))
        self.assertTrue(all(q.timestamp > 150 for _, qs in test for q in qs))

    def test_frozen_results_ignore_new_quotes_and_detect_tampering(self):
        sessions = [read_quotes(Path('data/demo_quotes.jsonl'), None)]
        qs = sessions[0][1]
        with TemporaryDirectory() as d:
            folder = freeze(sessions, None, Path(d), qs[130].timestamp, qs[-1].timestamp, True)
            original = (folder / 'report.json').read_bytes()
            sessions[0][1].append(Quote(qs[-1].timestamp + 5000, .01, .02, 1, 1))
            reproduce(folder)
            self.assertEqual((folder / 'report.json').read_bytes(), original)
            with (folder / 'inputs.json').open('ab') as out:
                out.write(b' ')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                reproduce(folder)

    def test_extended_features_compare_when_training_is_large_enough(self):
        _, sample = read_quotes(Path('data/demo_quotes.jsonl'), None)
        expanded = [Quote(q.timestamp + repeat * 2_000_000, q.bid, q.ask, q.bid_size, q.ask_size)
                    for repeat in range(5) for q in sample]
        settings = {'train_end_ms': expanded[len(sample) * 3 - 1].timestamp,
                    'test_end_ms': expanded[-1].timestamp, 'synthetic': True,
                    'delays_ms': [0], 'fees': [.01]}
        report = evaluate([('SYNTHETIC', expanded)], [], settings)
        extended = next(row for row in report['comparison'] if row['strategy'].endswith('price features'))
        self.assertGreaterEqual(extended['training_closed'], 30)
        self.assertIsNotNone(extended['predicted_if_filled'])
        self.assertGreater(extended['signals'], 0)

    def test_basket_gaps_and_missing_size_do_not_confirm(self):
        def scan(t, price=.3, size=1):
            return {'event_ticker': 'EVENT', 'ts_ms': t, 'legs': {'a': price, 'b': price, 'c': price},
                    'minimum_ask_size': size}
        result = basket_history([scan(0), scan(5000), scan(10000, size=0), scan(40000)], .01, 0)[0]
        self.assertEqual(result['candidate_snapshots'], 3)
        self.assertEqual(result['episodes'], 2)
        self.assertEqual(result['longest_observed_seconds'], 5)
        self.assertEqual(result['positive_later_with_size'], 1)
        self.assertEqual(result['unconfirmed'], 1)


class CollectionTests(unittest.TestCase):
    def test_series_failure_does_not_block_other_series(self):
        with TemporaryDirectory() as d:
            with patch('kalshi_record.open_events', side_effect=[OSError('unavailable'), []]) as api:
                record(['FIRST', 'SECOND'], Path(d), .000001, 1, Decimal('.01'), 1)
            self.assertEqual(api.call_count, 2)
            health = json.loads((Path(d) / 'health.json').read_text())
            self.assertEqual(health['series']['FIRST']['failures'], 1)
            self.assertIsNotNone(health['series']['SECOND']['last_success_ms'])

    def test_shared_storage_limit_stops_before_write(self):
        with TemporaryDirectory() as d:
            (Path(d) / 'test.quotes.jsonl').write_bytes(b'x' * (1024 * 1024))
            with patch('kalshi_record.open_events') as api:
                record(['FIRST'], Path(d), 1, 1, Decimal('.01'), 1)
            api.assert_not_called()
            health = json.loads((Path(d) / 'health.json').read_text())
            self.assertEqual(health['status'], 'storage limit')


if __name__ == '__main__':
    unittest.main()
