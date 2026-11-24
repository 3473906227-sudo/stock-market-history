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
        self.root = Path(self.tmp.name) / 'repo'
        self.output = self.root / 'data' / 'sectors'
        self.output.mkdir(parents=True, exist_ok=True)
        patch.object(s, 'OUT', self.output).start()
        patch.object(s, 'HIST', self.output / 'history').start()
        patch.object(s, 'clock', lambda: datetime(2026,11,21,20,tzinfo=timezone(timedelta(hours=8)))).start()
        s._API_BLOCKED = False
        s._API_LAST = 0
        s._API_SUSPECT = 0
        s._API_CALLS = 0

    def test_never_roll_back_verified_daily_closes(self):
        self.assertFalse(s._history_may_replace_quote('2026-11-17','2026-11-20'))
        self.assertFalse(s._history_may_replace_quote('2026-11-20','2026-11-24'))
        self.assertTrue(s._history_may_replace_quote('2026-11-24','2026-11-20'))
        self.assertTrue(s._history_may_replace_quote('2026-11-24','2026-11-24'))

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
            with self.assertRaises(s.MarketRateLimited):
                s._call_market(blocked)
            with self.assertRaises(s.MarketRateLimited):
                s._call_market(blocked)
        self.assertEqual(n[0],1)
        self.assertEqual(s._API_CALLS,1)

    def test_validate_checks_run_id_and_quality(self):
        s._write_status({'github_run_id':'a', 'ok':True, 'reason':'测试'})
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'b'}):
            self.assertEqual(s._validate_status(),2)
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'a'}):
            self.assertEqual(s._validate_status(),0)
        s._write_status({'github_run_id':'a','ok':False,'reason':'未达到交易日'})
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'a'}):
            self.assertEqual(s._validate_status(),2)


    def test_selection_keeps_17_groups_plus_stock_industry(self):
        groups = {'第%02d组'%i:['行业%02d'%i,'备用行业%02d'%i] for i in range(17)}
        s.save_json(s.OUT / 'selection.json', {'groups':groups})
        (self.root / 'stock_symbols.txt').write_text('000063\n000157\n',encoding='utf8')
        s.save_json(s.OUT / 'symbols.json',{'symbols':{
            '000063':'通信设备','000157':'工程机械','000999':'其他无关'}})
        allowed, tracked, refs, stocks = s._selection()
        self.assertEqual(len(refs),34)
        self.assertEqual(tracked,{'000063','000157'})
        self.assertEqual(stocks,{'通信设备','工程机械'})
        self.assertEqual(len(allowed),36)

    def test_prune_removes_unrelated_history_and_untracked_symbols(self):
        s.save_json(s.OUT / 'quotes.json', {'ok':True,'boards':[
            {'name':'通信设备'},{'name':'工程机械'},{'name':'无关板块'}]})
        s.save_json(s.OUT / 'symbols.json', {'symbols':{
            '000063':'通信设备','000157':'工程机械','000999':'无关板块'}})
        s.HIST.mkdir(parents=True,exist_ok=True)
        for name in ('通信设备','工程机械','无关板块'):
            (s.HIST/(s.digest(name)+'.csv')).write_text('date,close\n2026-11-17,1\n')
        result = s._prune_storage({'通信设备','工程机械'},{'000063','000157'})
        self.assertEqual(result['boards_removed'],1)
        self.assertEqual(result['symbols_removed'],1)
        self.assertEqual(result['history_files_removed'],1)
        self.assertEqual(len(list(s.HIST.glob('*.csv'))),2)
        self.assertEqual(len(s.load_json(s.OUT/'quotes.json')['boards']),2)
        self.assertNotIn('000999',s.load_json(s.OUT/'symbols.json')['symbols'])

    def test_selection_rejects_missing_whitelist(self):
        (self.root/'stock_symbols.txt').write_text('000063\n',encoding='utf8')
        with self.assertRaises(ValueError):
            s._selection()



    def test_official_bk_history_endpoint_without_industry_list(self):
        from unittest.mock import MagicMock
        import json
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps({'data':{
            'code':'BK1329', 'klines':['2026-11-20,100,101,102,99,10000,123456']
        }}).encode('utf-8')
        response.__enter__.return_value = response
        calls = []
        with patch.object(s,'urlopen',return_value=response) as web,\
             patch.object(s.pd,'DataFrame',side_effect=lambda rows,columns: (calls.append((rows,columns)) or rows),create=True):
            rows = s._direct_industry_history('BK1329',
                        datetime(2026,11,17,tzinfo=timezone(timedelta(hours=8))),
                        datetime(2026,11,22,tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual(rows[0][0],'2026-11-20')
        self.assertEqual(rows[0][2],'101')
        self.assertEqual(len(calls),1)
        self.assertIn('90.BK1329', web.call_args.args[0].full_url)

    def test_official_bk_history_rejects_mismatch(self):
        from unittest.mock import MagicMock
        import json
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps({'data':{
            'code':'BK0000','klines':['2026-11-20,100,101,102,99,10000']
        }}).encode('utf-8')
        response.__enter__.return_value = response
        with patch.object(s,'urlopen',return_value=response):
            with self.assertRaises(ValueError):
                s._direct_industry_history('BK1329',
                    datetime(2026,11,17,tzinfo=timezone(timedelta(hours=8))),
                    datetime(2026,11,22,tzinfo=timezone(timedelta(hours=8))))

    def test_daily_gate_one_attempt_per_trade_day(self):
        from tools.sector_daily_gate import should_run, expected_close_day, CN
        friday = datetime(2026, 11, 20, 17, 15, tzinfo=CN)
        weekend = datetime(2026, 11, 22, 9, 0, tzinfo=CN)
        self.assertEqual(expected_close_day(friday), '2026-11-20')
        self.assertEqual(expected_close_day(weekend), '2026-11-20')
        old = {'expected_trading_day':'2026-11-20', 'mode':'full', 'ok':False}
        self.assertFalse(should_run('2026-11-20',old,event='schedule')[0])
        self.assertFalse(should_run('2026-11-20',old,event='workflow_dispatch')[0])
        self.assertTrue(should_run('2026-11-20',old,event='workflow_dispatch',force=True)[0])
        self.assertTrue(should_run('2026-11-23',old,event='schedule')[0])
        self.assertFalse(should_run('2026-11-20',old,event='schedule',force=True)[0])

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
             patch.object(s, 'returns', lambda *a,**kw:None),\
             patch.object(s, '_history_asof', get_asof),\
             patch.object(s, '_tracked_sectors', lambda:{'通信设备'}),\
             patch.object(s, '_selection', lambda:(set(x['name'] for x in boards),set(),set(),set())),\
             patch.object(s, '_prune_storage', lambda *args:{}),\
             patch.object(sys, 'argv',['sector_sync.py','--max-boards','28','--max-members','0']):
            self.assertEqual(s.main(),0)
        return s.load_json(s.OUT / 'status.json')

    def test_stale_not_green_even_when_fetch_returns(self):
        status = self._fake_full(fresh=False)
        self.assertFalse(status['ok'])
        self.assertEqual(status['core_stale_count'],1)
        self.assertEqual(status['history_fresh_success'],0)

    def test_core_verified_batch_advances_but_pending_is_not_full_success(self):
        status = self._fake_full(fresh=True)
        self.assertFalse(status['ok'])
        self.assertEqual(status['core_stale_count'],0)
        self.assertEqual(status['history_fresh_success'],28)
        self.assertEqual(status['history_pending_count'],2)
        self.assertIn('待分批补齐',status['reason'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
