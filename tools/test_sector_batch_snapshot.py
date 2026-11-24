#!/usr/bin/env python3
"""官方BK批量报价：脱机回归，严格核对交易时间和历史基准。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from tools import sector_batch_snapshot as batch

TS=1795505997 # 2026-11-24 15:39:57北京时间，真实接口时间格式
DAY="2026-11-24"
ROW={"f2":2926.36,"f3":2.32,"f12":"BK1329","f13":90,
     "f14":"集成电路制造","f124":TS}

class TestBatchSnapshot(unittest.TestCase):
    def test_validation_fails_for_stale_or_other_market_quotes(self):
        self.assertEqual(batch.validate_row(ROW,{"BK1329"},DAY)["close"],2926.36)
        for change in ({"f12":"BK9999"},{"f13":1},{"f2":"-"},{"f3":"-"},
                       {"f124":1795185997},{"f124":0}):
            item={**ROW,**change}
            with self.assertRaises(ValueError):
                batch.validate_row(item,{"BK1329"},DAY)

    def test_multi_code_single_request_returns_data_without_retry(self):
        reply={"rc":0,"data":{"total":2,"diff":[ROW,{**ROW,"f12":"BK0422",
                 "f2":6052.47,"f3":-0.45,"f14":"物流"}]}}
        response=MagicMock()
        response.status=200
        response.read.return_value=json.dumps(reply).encode()
        response.__enter__.return_value=response
        with patch.object(batch,"urlopen",return_value=response) as web:
            rows=batch._api_rows(["BK1329","BK0422"])
        self.assertEqual(len(rows),2)
        self.assertEqual(web.call_count,1)
        self.assertIn("90.BK1329",web.call_args.args[0].full_url)

    def test_secondary_official_host_after_primary_tls_timeout(self):
        from urllib.error import URLError
        reply={"rc":0,"data":{"total":1,"diff":[ROW]}}
        response=MagicMock()
        response.status=200
        response.read.return_value=json.dumps(reply).encode()
        response.__enter__.return_value=response
        with patch.object(batch,"urlopen",side_effect=[TimeoutError("TLS handshake timed out"),response]) as web:
            rows=batch._api_rows(["BK1329"])
        self.assertEqual(rows[0]["f12"],"BK1329")
        self.assertEqual(web.call_count,2)
        self.assertIn("push2.eastmoney.com",web.call_args_list[0].args[0].full_url)
        self.assertIn("push2delay.eastmoney.com",web.call_args_list[1].args[0].full_url)

    def test_both_hosts_fail_closed_not_data_fabricated(self):
        with patch.object(batch,"urlopen",side_effect=TimeoutError("TLS handshake timed out")) as web:
            with self.assertRaises(IOError):
                batch._api_rows(["BK1329"])
        self.assertEqual(web.call_count,2)

    def test_one_daily_close_preserves_real_past_prices_and_old_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            hist=Path(root)/"history"
            closes=Path(root)/"close_history"
            hist.mkdir()
            import hashlib
            digest=hashlib.sha256("集成电路制造".encode()).hexdigest()[:20]
            (hist/(digest+".csv")).write_text(
                "date,close\n2026-11-17,2500\n2026-10-27,2300\n",encoding="utf8")
            board={"code":"BK1329","name":"集成电路制造",
                   "history_asof":"2026-11-20","history_close":2872.97,
                   "ret5":-999,"ret20":-999}
            x=batch.validate_row(ROW,{"BK1329"},DAY)
            changed=batch.apply_batch_close(board,x,DAY,hist,closes,
                                      {5:"2026-11-17",20:"2026-10-27"})
            self.assertTrue(changed)
            self.assertEqual(board["history_asof"],DAY)
            self.assertEqual(board["quote_basis"],"official_bk_batch_close")
            self.assertAlmostEqual(board["ret5"],round((2926.36/2500-1)*100,3))
            self.assertAlmostEqual(board["ret20"],round((2926.36/2300-1)*100,3))
            self.assertFalse(board["ohlcv_complete_to_target"])
            self.assertTrue(list(closes.glob("*.csv")))
            self.assertFalse(batch.apply_batch_close(board,x,DAY,hist,closes,
                                                {5:"2026-11-17",20:"2026-10-27"}))

if __name__=="__main__":
    unittest.main()
