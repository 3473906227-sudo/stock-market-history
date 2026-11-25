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


    def test_bad_first_large_batch_splits_and_recovers_all(self):
        code_list=["BK%04d"%(1000+i) for i in range(10)]
        rows=[{**ROW,"f12":code,"f2":1000+i} for i,code in enumerate(code_list)]
        boards=[{"code":code,"name":"测试%d"%i,"history_asof":"2026-11-20"}
                for i,code in enumerate(code_list)]
        calls=[]
        def fetch(part):
            calls.append(part)
            if len(part)>5:
                raise TimeoutError("url too long")
            return [row for row in rows if row["f12"] in part]
        def apply(board,quote,expected,history,close,refs):
            board["history_asof"]=expected
            board["close"]=quote["close"]
            return True
        with patch.object(batch,"_api_rows",side_effect=fetch), \
             patch.object(batch,"apply_batch_close",side_effect=apply), \
             patch.object(batch,"reference_days",return_value={5:"2026-11-17",20:"2026-10-27"}), \
             patch.object(batch.time,"sleep",return_value=None):
            result=batch.update(boards,expected_day=DAY,history_dir="/unused",
                                close_dir="/unused",chunk_size=10)
        self.assertEqual(result["recovered"],10)
        self.assertEqual(result["attempted"],3)
        self.assertEqual(result["missing_count"],0)
        self.assertTrue(all(x["history_asof"]==DAY for x in boards))

    def test_early_failed_batch_does_not_skip_other_bk_groups(self):
        code_list=["BK%04d"%(1100+i) for i in range(10)]
        boards=[{"code":code,"name":"测试%d"%i,"history_asof":"2026-11-20"}
                for i,code in enumerate(code_list)]
        def fetch(part):
            if part[0]==code_list[0]:
                raise TimeoutError("source timed out")
            return [{**ROW,"f12":code,"f2":1000+i}
                    for i,code in enumerate(code_list) if code in part]
        def apply(board,quote,expected,history,close,refs):
            board["history_asof"]=expected
            return True
        with patch.object(batch,"_api_rows",side_effect=fetch), \
             patch.object(batch,"apply_batch_close",side_effect=apply), \
             patch.object(batch,"reference_days",return_value={5:"2026-11-17",20:"2026-10-27"}), \
             patch.object(batch.time,"sleep",return_value=None):
            result=batch.update(boards,expected_day=DAY,history_dir="/unused",
                                close_dir="/unused",chunk_size=5)
        self.assertEqual(result["recovered"],5)
        self.assertEqual(result["missing_count"],5)
        self.assertEqual(result["attempted"],2)

    def test_next_trading_day_official_f18_matches_known_previous_close(self):
        next_ts=1795569441
        current={**ROW,"f124":next_ts,"f18":1315.04,"f12":"BK0731",
                 "f2":1306.53,"f3":-0.65}
        with patch.object(batch,"current_china_date",return_value="2026-11-25"):
            item=batch.validate_previous_close(current,{"BK0731"},"2026-11-24")
            self.assertEqual(item["close"],1315.04)
            self.assertEqual(item["day"],"2026-11-24")
            with self.assertRaises(ValueError):
                batch.validate_previous_close({**current,"f18":None},
                                              {"BK0731"},"2026-11-24")
            with self.assertRaises(ValueError):
                batch.validate_previous_close({**current,"f124":TS},
                                              {"BK0731"},"2026-11-24")

    def test_backfill_previous_day_only_after_matching_official_anchor(self):
        anchor={"code":"BK0731","name":"农化制品","history_asof":"2026-11-24",
                "history_close":1315.04,"last":1315.04,
                "quote_basis":"official_daily_close"}
        pending=[{"code":"BK%04d"%(1500+i),"name":"演示行业%d"%i,
                  "history_asof":"2026-11-20","change_pct":7.0}
                 for i in range(3)]
        boards=[anchor,*pending]
        f18={b["code"]:2000+i for i,b in enumerate(pending)}
        ts=1795569441
        calls=[]
        def fetch(part):
            calls.append(part)
            rows=[]
            if anchor["code"] in part:
                rows.append({**ROW,"f12":"BK0731","f2":1306.53,"f18":1315.04,
                             "f124":ts})
            for code in part:
                if code in f18:
                    rows.append({**ROW,"f12":code,"f18":f18[code],
                                 "f2":f18[code]-4.0,"f124":ts})
            return rows
        with tempfile.TemporaryDirectory() as root:
            with patch.object(batch,"_api_rows",side_effect=fetch), \
                 patch.object(batch,"current_china_date",return_value="2026-11-25"), \
                 patch.object(batch.time,"sleep",return_value=None):
                result=batch.update(boards,expected_day="2026-11-24",
                                    history_dir=Path(root)/"history",
                                    close_dir=Path(root)/"close_history",
                                    chunk_size=3)
        self.assertEqual(result["recovered"],3)
        self.assertTrue(result["previous_close_mode"])
        self.assertEqual(result["missing_count"],0)
        self.assertEqual(anchor["history_close"],1315.04)
        for item in pending:
            self.assertEqual(item["history_asof"],"2026-11-24")
            self.assertEqual(item["quote_basis"],"official_bk_previous_close")
            self.assertIsNone(item["change_pct"])
            self.assertIsNone(item["ret5"])

    def test_f18_recovery_never_proceeds_on_mismatched_anchor(self):
        anchor={"code":"BK0731","name":"农化制品","history_asof":"2026-11-24",
                "history_close":1315.04,"quote_basis":"official_daily_close"}
        pending={"code":"BK1500","name":"未更新行业","history_asof":"2026-11-20"}
        row={**ROW,"f12":"BK0731","f18":999.0,"f124":1795569441}
        with patch.object(batch,"_api_rows",return_value=[row]), \
             patch.object(batch,"current_china_date",return_value="2026-11-25"), \
             patch.object(batch.time,"sleep",return_value=None):
            result=batch.update([anchor,pending],expected_day="2026-11-24",
                                history_dir="/unread",close_dir="/unread",
                                chunk_size=3)
        self.assertEqual(result["recovered"],0)
        self.assertFalse(result["previous_close_mode"])
        self.assertEqual(pending["history_asof"],"2026-11-20")

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
