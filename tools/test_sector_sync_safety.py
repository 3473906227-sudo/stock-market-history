#!/usr/bin/env python3
"""无需联网或行情依赖的行业同步安全回归测试。"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# 被测模块的外部行情依赖仅在实际下载时才使用。
sys.modules.setdefault('akshare', types.ModuleType('akshare'))
sys.modules.setdefault('pandas', types.ModuleType('pandas'))
import sector_sync as s

class SectorSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(s, 'OUT', Path(self.tmp.name)).start()
        patch.object(s, 'HIST', Path(self.tmp.name) / 'history').start()
        patch.object(s, 'clock', lambda: datetime(2026,11,21,20,tzinfo=timezone(timedelta(hours=8)))).start()
        s._API_BLOCKED = False
        s._API_LAST = 0
        s._API_SUSPECT = 0
        s._API_CALLS = 0

    def test_weekend_expected_close_is_friday(self):
        self.assertEqual(s._expected_day(), '2026-11-20')
        with patch.object(s, 'clock', lambda:datetime(2026,11,20,14,tzinfo=timezone(timedelta(hours=8)))):
            self.assertEqual(s._expected_day(), '2026-11-19')

    def test_consecutive_rate_limits_open_breaker(self):
        n = [0]
        def blocked():
            n[0] += 1
            raise RuntimeError('东方财富 push2 行情服务器断开连接')
        with patch.object(s.time, 'sleep', lambda _:None):
            with self.assertRaises(RuntimeError):
                s._call_market(blocked)
            with self.assertRaises(s.MarketRateLimited):
                s._call_market(blocked)
            with self.assertRaises(s.MarketRateLimited):
                s._call_market(blocked)
        self.assertEqual(n[0],2)
        self.assertEqual(s._API_CALLS,2)

    def test_validate_checks_run_id_and_quality(self):
        s._write_status({'github_run_id':'a', 'ok':True, 'reason':'测试'})
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'b'}):
            self.assertEqual(s._validate_status(),2)
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'a'}):
            self.assertEqual(s._validate_status(),0)
        s._write_status({'github_run_id':'a','ok':False,'reason':'未达到交易日'})
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'a'}):
            self.assertEqual(s._validate_status(),2)

    def _fake_full(self, *, fresh):
        from collections import defaultdict
        current = defaultdict(lambda:'2026-11-17')
        boards = [{'name':n,'code':'BK0001','change_pct':0.2,'last':100} for n in
                  ['通信设备'] + ['测试行业%02d'%k for k in range(29)]]
        def get_asof(name):
            return current[name]
        def fake_history(name,_code=None):
            if fresh:
                current[name]='2026-11-20'
            return 400
        with patch.object(s, 'live_quotes', lambda:boards),\
             patch.object(s, 'history', fake_history),\
             patch.object(s, 'returns', lambda board:None),\
             patch.object(s, '_history_asof', get_asof),\
             patch.object(s, '_tracked_sectors', lambda:{'通信设备'}),\
             patch.object(sys, 'argv',['sector_sync.py','--max-boards','28','--max-members','0']):
            self.assertEqual(s.main(),0)
        return s.load_json(s.OUT / 'status.json')

    def test_stale_not_green_even_when_fetch_returns(self):
        status = self._fake_full(fresh=False)
        self.assertFalse(status['ok'])
        self.assertEqual(status['core_stale_count'],1)
        self.assertEqual(status['history_fresh_success'],0)

    def test_core_verified_batch_can_advance_and_marks_pending(self):
        status = self._fake_full(fresh=True)
        self.assertTrue(status['ok'])
        self.assertEqual(status['core_stale_count'],0)
        self.assertEqual(status['history_fresh_success'],28)
        self.assertEqual(status['history_pending_count'],2)
        self.assertIn('待分批补齐',status['reason'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
