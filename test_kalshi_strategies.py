from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kalshi_arbitrage import complete_la_event, scan_basket
from kalshi_paper import Quote, read_quotes, split_sessions


class BasketTests(unittest.TestCase):
    def setUp(self):
        ticker = 'KXHIGHLAX-26NOV11'
        self.event = {'event_ticker': ticker, 'mutually_exclusive': True, 'markets': [
            {'ticker': ticker + '-' + str(i), 'event_ticker': ticker,
             'strike_type': kind, 'floor_strike': floor, 'cap_strike': cap,
             'status': 'active', 'yes_ask_dollars': '0.30', 'yes_ask_size_fp': '1'}
            for i, (kind, floor, cap) in enumerate([
                ('less', None, 80), ('between', 80, 81), ('greater', 81, None)])]}

    def test_candidate_accounts_for_every_leg_fee(self):
        result = scan_basket(self.event, Decimal('0.01'), 123)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(Decimal(result['net_if_filled']), Decimal('0.07'))
        self.assertEqual(scan_basket(self.event, Decimal('0.04'), 123)['status'], 'no edge')

    def test_gap_overlap_and_missing_tail_are_rejected(self):
        for floor in (79, 81):
            event = deepcopy(self.event)
            event['markets'][1]['floor_strike'] = floor
            self.assertFalse(complete_la_event(event))
        self.event['markets'].pop()
        self.assertFalse(complete_la_event(self.event))

    def test_unavailable_leg_is_not_a_candidate(self):
        self.event['markets'][0]['yes_ask_size_fp'] = '0'
        self.assertNotEqual(scan_basket(self.event, Decimal('0.01'), 123)['status'], 'candidate')


class QuoteTests(unittest.TestCase):
    def test_live_read_ignores_only_unfinished_tail(self):
        row = {'market_ticker': 'TEST', 'ts_ms': 1000,
               'yes_bid_dollars': '0.4', 'yes_ask_dollars': '0.5',
               'yes_bid_size_fp': '1', 'yes_ask_size_fp': '1'}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'quotes.jsonl'
            path.write_text(json.dumps(row) + '\n' + '{unfinished')
            self.assertEqual(len(read_quotes(path, None, live=True)[1]), 1)
            with self.assertRaises(ValueError):
                read_quotes(path, None)
            path.write_text(json.dumps(row) + '\n' + '{broken}\n')
            with self.assertRaises(ValueError):
                read_quotes(path, None, live=True)

    def test_event_split_excludes_overlapping_training_time(self):
        def quotes(*times):
            return [Quote(t, .4, .5, 1, 1) for t in times]
        earlier, later = split_sessions([
            ('KXHIGHLAX-26NOV11-B80', quotes(100, 300)),
            ('KXHIGHLAX-26NOV11-B82', quotes(100, 400)),
            ('KXHIGHLAX-26NOV12-B80', quotes(200, 400, 500)),
        ])
        self.assertEqual(len(earlier), 2)
        self.assertEqual([q.timestamp for _, qs in later for q in qs], [500])


if __name__ == '__main__':
    unittest.main()
