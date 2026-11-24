#!/usr/bin/env python3
"""备用源和数据日期的脱机回归测试，不访问行情网络。"""
from __future__ import annotations
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import hashlib
import json
import gzip
from tools import sector_daily_backup as backup

PAGE = """<html><body>
<section>行业板块</section><span>·</span><b>BK1329</b><span>· CN</span>
<h1>集成电路制造</h1>
<p>沪深 A 股行业板块与概念板块，交易时段每 5 分钟刷新，展示涨跌幅、成交额、涨跌家数与领涨股。</p>
<div>2,872.97</div><div>-3.91%</div><div>更新于</div><div>2026-11-24 15:10</div>
<div>涨跌幅</div><div>-3.91%</div><div>收盘</div><div>2,872.97</div>
</body></html>"""

class BackupTests(TestCase):
    def test_exact_code_date_and_secondary_close_are_required(self):
        quote = backup.parse_snapshot(PAGE, "BK1329", "2026-11-24")
        self.assertEqual(quote["close"], 2872.97)
        self.assertEqual(quote["pct"], -3.91)
        for bad in [
            (PAGE.replace("BK1329","BK9999"),"BK1329","2026-11-24"),
            (PAGE,"BK1329","2026-11-25"),
            (PAGE.replace("收盘</div><div>2,872.97","收盘</div><div>9,872.97"),
             "BK1329","2026-11-24"),
            (PAGE.replace("2026-11-24 15:10","2026-11-24 13:25"),
             "BK1329","2026-11-24"),
        ]:
            with self.assertRaises(ValueError):
                backup.parse_snapshot(*bad)

    def test_real_close_newer_than_previous_and_missing_returns_are_null(self):
        with TemporaryDirectory() as directory:
            item={"name":"集成电路制造","code":"BK1329",
                  "history_asof":"2026-11-20","ret5":12.1,"ret20":13.4}
            quote=backup.parse_snapshot(PAGE,"BK1329","2026-11-24")
            refs={5:"2026-11-17",20:"2026-10-27"}
            self.assertTrue(backup.apply_snapshot(item,quote,refs,Path(directory)))
            self.assertEqual(item["history_asof"],"2026-11-24")
            self.assertIsNone(item["ret5"])
            self.assertIsNone(item["ret20"])
            self.assertEqual(item["quote_basis"],"third_party_verified_eastmoney_BK_close")
            self.assertFalse(item["ohlcv_complete_to_target"])
            self.assertFalse(backup.apply_snapshot(item,quote,refs,Path(directory)))

    def test_verified_history_baselines_calculate_real_5_and_20_day_returns(self):
        with TemporaryDirectory() as directory:
            name="集成电路制造"
            code=hashlib.sha256(name.encode("utf8")).hexdigest()[:20]
            path=Path(directory)/(code+".csv")
            path.write_text("date,close\n2026-11-17,2500\n2026-10-27,2300\n",encoding="utf8")
            quote=backup.parse_snapshot(PAGE,"BK1329","2026-11-24")
            item={"name":name,"code":"BK1329","history_asof":"2026-11-20"}
            backup.apply_snapshot(item,quote,{5:"2026-11-17",20:"2026-10-27"},directory)
            self.assertAlmostEqual(item["ret5"],round((2872.97/2500-1)*100,3))
            self.assertAlmostEqual(item["ret20"],round((2872.97/2300-1)*100,3))

    def test_old_backup_pages_stop_after_five_and_do_not_change_quotes(self):
        rows=[{"name":f"Test{i}","code":f"BK{i+1000}","history_asof":"2026-11-20"}
              for i in range(15)]
        old=[dict(row) for row in rows]
        with patch.object(backup,'download',return_value=PAGE), \
             patch.object(backup,'reference_days',return_value={5:"2026-11-17",20:"2026-10-27"}):
            result=backup.update_missing(rows,expected_day="2026-11-25",
                                         history_dir="/tmp/unread",sleep_seconds=0)
        self.assertEqual(result['recovered'],0)
        self.assertEqual(result['attempted'],5)
        self.assertTrue(result['stale_source'])
        self.assertEqual(rows,old)
