import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import sector_ohlcv as sync


TARGET = '2026-11-24'
BOARD = {'code': 'BK1220', 'name': '广告营销', 'quote_asof': TARGET, 'last': 103.0}
ANCHOR = '2026-11-17,95,98,101,93,120'
LATEST = '2026-11-24,102,103,111,99,150,45000'
ANCHOR_VALUES = ('95', '101', '93', '98', '120')


def payload(*rows, **changes):
    return {'rc': 0, 'data': dict(code=BOARD['code'], market=90, name=BOARD['name'], klines=list(rows)) | changes}


class SectorHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name)
        self.inbox = self.out / 'artifacts'
        self.inbox.mkdir()
        patcher = patch.object(sync, 'OUT', self.out)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.local = {'2026-11-17': ANCHOR_VALUES}
        self.path = sync._history_file(BOARD, self.out / 'history')
        sync.write_history(self.path, self.local)

    def test_eastmoney_order_mapping_and_rejection(self):
        rows, rejected = sync.parse_payload(payload(
            LATEST, '2026-11-22,100,110,111,90,100', '2026-11-25,100,110,111,90,100',
            '2026-11-20,100,110,104,90,100', '2026-11-19,100,110,111,90,NaN'), BOARD, TARGET)
        self.assertEqual(rows, {TARGET: ('102', '111', '99', '103', '150')})
        self.assertEqual(len(rejected), 4)
        for bad in [payload(LATEST, code='BK1111'), payload(LATEST, market=1), payload(LATEST, name='其他行业')]:
            with self.assertRaises(ValueError):
                sync.parse_payload(bad, BOARD, TARGET)

    def test_requires_consistent_overlap_with_original_csv(self):
        with self.assertRaisesRegex(ValueError, '共同交易日'):
            sync.verified_rows(payload(LATEST), BOARD, TARGET, self.local, set(self.local))
        with self.assertRaisesRegex(ValueError, '不一致'):
            sync.verified_rows(payload(ANCHOR.replace(',120', ',140'), LATEST), BOARD, TARGET, self.local, set(self.local))
        rows, _ = sync.verified_rows(payload(ANCHOR, LATEST), BOARD, TARGET, self.local, set(self.local))
        self.assertEqual(len(rows), 2)

    def test_backfill_requires_current_ohlcv_and_recorded_past_close_when_old_bars_conflict(self):
        self.local[TARGET] = ('102', '111', '99', '103', '150')
        gap = '2026-11-20,102,103,111,99,150'
        source = payload(ANCHOR.replace(',120', ',140'), gap, LATEST)
        with self.assertRaisesRegex(ValueError, '独立收盘价'):
            sync.verified_rows(source, BOARD, TARGET, self.local, set(self.local))
        sync._save_close_prices(BOARD, self.out / 'close_history', {'2026-11-20': 103})
        attempts = [{'endpoint': sync.ENDPOINTS[0], 'payload': source}]
        errors = []
        additions, _ = sync.merge_board(BOARD, self.local, set(self.local), TARGET, attempts, errors)
        self.assertEqual(set(additions), {'2026-11-20'})
        self.assertIn('保留原记录', errors[0])

    def test_two_official_responses_disagree_on_new_bar(self):
        attempts = [{'endpoint': sync.ENDPOINTS[0], 'payload': payload(ANCHOR, LATEST)},
                    {'endpoint': sync.ENDPOINTS[1], 'payload': payload(ANCHOR, LATEST.replace(',150,', ',999,'))}]
        errors = []
        additions, _ = sync.merge_board(BOARD, self.local, set(self.local), TARGET, attempts, errors)
        self.assertEqual(additions, {})
        self.assertIn(TARGET, errors[0])

    def test_closed_snapshot_must_have_real_fields_and_an_independent_previous_close(self):
        final_time = int(datetime(2026, 11, 24, 15, 30, tzinfo=sync.CN).timestamp())
        row = {'f12': BOARD['code'], 'f13': 90, 'f14': BOARD['name'], 'f124': final_time,
               'f17': 102, 'f15': 111, 'f16': 99, 'f2': 103, 'f5': 150, 'f18': 100, 'f3': 3.0}
        self.assertEqual(sync.snapshot_bar(BOARD, row, TARGET, 100), ('102', '111', '99', '103', '150'))
        for invalid in [dict(row, f124=final_time - 3600), dict(row, f124=final_time + 86400),
                        dict(row, f18=101), dict(row, f5='-'), dict(row, f3=2.0), dict(row, f14='其他行业')]:
            with self.assertRaises(ValueError):
                sync.snapshot_bar(BOARD, invalid, TARGET, 100)
        with self.assertRaises(ValueError):
            sync.snapshot_bar(BOARD, row, TARGET, None)
        errors = []
        observations = [(sync.SNAPSHOT_ENDPOINTS[0], row), (sync.SNAPSHOT_ENDPOINTS[1], dict(row, f5=151))]
        self.assertIsNone(sync.verified_snapshot(BOARD, TARGET, 100, observations, errors))
        self.assertIn('不一致', errors[0])

    def test_publish_partial_ohlcv_and_alert_even_when_quotes_are_complete(self):
        boards = [dict(BOARD)] + [dict(BOARD, code=f'BK{3000+i}', name=f'行业{i}') for i in range(99)]
        sync.apply_sector_pages.atomic_json(self.out / 'quotes.json', {'boards': boards})
        sync.apply_sector_pages.atomic_json(self.inbox / 'shard.json', {'target': TARGET, 'boards': {
            BOARD['code']: {'responses': [{'endpoint': sync.ENDPOINTS[0], 'payload': payload(ANCHOR, LATEST)}]},
        }})
        sync.publish(TARGET, self.inbox)
        status = json.loads((self.out / 'status.json').read_text(encoding='utf8'))
        self.assertEqual(status['quote_fresh_count'], 100)
        self.assertEqual(status['history_fresh_count'], 1)
        self.assertEqual(status['history_gap_board_count'], 100)
        self.assertEqual(status['history_gaps'][0]['dates'], ['2026-11-18', '2026-11-19', '2026-11-20', '2026-11-23'])
        self.assertFalse(status['ok'])
        self.assertEqual(sync.read_history(self.path)['2026-11-17'], ANCHOR_VALUES)
        self.assertEqual(sync.read_history(self.path)[TARGET], ('102', '111', '99', '103', '150'))
        with patch('builtins.print'):
            self.assertEqual(sync.check(TARGET), 2)
            self.assertEqual(sync.check(TARGET, quotes_only=True), 0)
        first = (self.path.read_bytes(), sync._history_file(BOARD, self.out / 'close_history').read_bytes())
        sync.publish(TARGET, self.inbox)
        self.assertEqual(first, (self.path.read_bytes(), sync._history_file(BOARD, self.out / 'close_history').read_bytes()))


if __name__ == '__main__':
    unittest.main()
