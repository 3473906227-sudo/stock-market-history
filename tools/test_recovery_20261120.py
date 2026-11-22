#!/usr/bin/env python3
"""应急行情抓取离线回归：绝不拼造收盘价或历史K线。"""
import unittest
from datetime import date
from tools.recover_sectors_20261120 import parse_verified_html,reference_dates

BASE="""<html><body>
<div>行业板块</div><div>·</div><span>BK1329</span><span>· CN</span><h1>集成电路制造</h1>
<p>沪深 A 股行业板块与概念板块，交易时段每 5 分钟刷新，展示涨跌幅、成交额、涨跌家数与领涨股。</p>
<div>2,872.97</div><div>-3.91%</div><div>更新于</div><div>2026-11-20 15:10</div>
<div>涨跌幅</div><div>-3.91%</div><div>成交额</div><div>246.4亿</div><div>收盘</div><div>2,872.97</div>
</body></html>"""

class RecoveryValidation(unittest.TestCase):
    def test_real_snapshot_must_match_date_code_name_and_close(self):
        quote=parse_verified_html(BASE,"集成电路制造","BK1329")
        self.assertEqual(quote["trade_date"],"2026-11-20")
        self.assertEqual(quote["close"],2872.97)
        self.assertEqual(quote["change_pct"],-3.91)
        for case in (BASE.replace("BK1329","BK0000"),
                     BASE.replace("2026-11-20","2026-11-17"),
                     BASE.replace("收盘</div><div>2,872.97","收盘</div><div>3,872.97")):
            with self.assertRaises(ValueError):
                parse_verified_html(case,"集成电路制造","BK1329")
    def test_exchange_calendar_reference_dates_not_guesswork(self):
        days=reference_dates("2026-11-20")
        self.assertEqual(days[5],"2026-11-13")
        self.assertEqual(days[20],"2026-10-23")

if __name__=="__main__":
    unittest.main()
