#!/usr/bin/env python3
"""应急行情抓取离线回归：绝不拼造收盘价或历史K线。"""
import unittest
from datetime import date
import json
import gzip
from unittest.mock import MagicMock
from pathlib import Path
import tempfile
from unittest.mock import patch
from tools import recover_sectors_20261120 as rec
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
    def test_official_complete_history_recovers_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            quote_file=root/"quotes.json"
            status_file=root/"status.json"
            board={"name":"官方已有日K","code":"BK1234","history_asof":"2026-11-17"}
            items=[board]+[{"name":"already"+str(i),"code":"BK%04d"%(1000+i),
                            "history_asof":"2026-11-20"} for i in range(116)]
            quote_file.write_text(json.dumps({"boards":items},ensure_ascii=False),encoding="utf8")
            refs=reference_dates("2026-11-20",back_counts=(1,5,20))
            past={refs[1]:100,refs[5]:95,refs[20]:80,"2026-11-20":102}
            with (
                patch.object(rec,'QUOTES',quote_file),
                patch.object(rec,'STATUS',status_file),
                patch.object(rec,'historical_reference',return_value=past),
                patch.object(rec,'get_page',side_effect=AssertionError("不得请求第三方")),
            ):
                self.assertEqual(rec.main(),0)
            updated=json.loads(quote_file.read_text("utf8"))
            stat=json.loads(status_file.read_text("utf8"))
            self.assertEqual(updated["boards"][0]["history_asof"],"2026-11-20")
            self.assertEqual(updated["boards"][0]["quote_basis"],"official_daily_close")
            self.assertEqual(updated["boards"][0]["history_ohlcv_asof"],"2026-11-20")
            self.assertEqual(stat["official_recovered_count"],1)
            self.assertEqual(stat["fresh_total"],117)
            self.assertEqual(stat["network_requests"],0)
    def test_verified_same_bk_code_allows_site_alias(self):
        altered=BASE.replace("集成电路制造","集成电路制造Ⅲ")
        quote=parse_verified_html(altered,"集成电路制造","BK1329")
        self.assertEqual(quote["source_label"],"集成电路制造Ⅲ")
        self.assertEqual(quote["close"],2872.97)

    def test_missing_historic_reference_does_not_reuse_stale_returns(self):
        board={"name":"示例","code":"BK1329","ret5":8.8,"ret20":9.9}
        ref={1:"2026-11-19",5:"2026-11-13",20:"2026-10-23"}
        rec._apply_quote(board,close=2872.97,pct=-3.91,past={},reference=ref,
                         source="核验来源",basis="third_party_verified_eastmoney_BK_close")
        self.assertEqual(board["history_asof"],"2026-11-20")
        self.assertEqual(board["change_pct"],-3.91)
        self.assertIsNone(board["ret5"])
        self.assertIsNone(board["ret20"])
        self.assertEqual(board["history_ohlcv_asof"],"")

    def test_gzip_page_decodes_without_guessing_prices(self):
        html=BASE+"".join("<p>行情采集片段%d_%d</p>"%(i,i*i*17) for i in range(400))
        compressed=gzip.compress(html.encode("utf-8"))
        self.assertGreater(len(compressed),2000)
        response=MagicMock()
        response.status=200
        response.read.return_value=compressed
        response.__enter__.return_value=response
        with patch.object(rec,"urlopen",return_value=response):
            recovered=rec.get_page("BK1553")
        self.assertIn("2026-11-20 15:10",recovered)
        self.assertIn("2,872.97",recovered)

    def test_exchange_calendar_reference_dates_not_guesswork(self):
        days=reference_dates("2026-11-20")
        self.assertEqual(days[5],"2026-11-13")
        self.assertEqual(days[20],"2026-10-23")

if __name__=="__main__":
    unittest.main()
