#!/usr/bin/env python3
"""五个独立BK分页结果脱机测试，验证完整白名单与日期，不联网。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import apply_sector_pages as a
from tools import sector_batch_snapshot as b

TS=1795569441

class PageAggregationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.out=self.root/"data"/"sectors"
        self.out.mkdir(parents=True,exist_ok=True)
        self.pages=self.root/"pages"
        self.pages.mkdir()
        self.boards=[{
            "code":"BK%04d"%(2000+i),
            "name":"有效行业%03d"%i,
            "history_asof":"2026-11-20",
            "history_close":1500.0,
            "change_pct":3.33,
        } for i in range(116)]
        self.anchor={"code":"BK0731","name":"农化制品",
                     "history_asof":"2026-11-24","history_close":1315.04,
                     "quote_basis":"official_daily_close","last":1315.04}
        self.boards.append(self.anchor)
        (self.out/"quotes.json").write_text(
            json.dumps({"boards":self.boards,"fetched_at":"2026-11-22"},ensure_ascii=False),
            encoding="utf8")
        patch.object(a,"OUT",self.out).start()
        patch.object(a,"HIST",self.out/"history").start()
        patch.object(a,"CLOSE_HISTORY",self.out/"close_history").start()
        patch.object(a,"current_china_day",return_value="2026-11-25").start()
        self.addCleanup(patch.stopall)

    def seed_pages(self,missing_last=False,wrong_anchor=False):
        rows=[]
        for i,x in enumerate(self.boards):
            price=1315.04 if x["code"]=="BK0731" else 1700+i*3
            if wrong_anchor and x["code"]=="BK0731":
                price=999.99
            rows.append({"f12":x["code"],"f13":90,"f14":x["name"],
                         "f18":price,"f2":price-9,"f3":-0.5,"f124":TS})
        for p in range(1,6):
            chunk=rows[(p-1)*24:p*24]
            if p==5 and missing_last:
                continue
            (self.pages/f"page_{p}.json").write_text(
                json.dumps({"page":p,"ok":True,"total":496,"rows":chunk}),
                encoding="utf8")

    def test_all_five_pages_restore_116_missing_using_verified_f18(self):
        self.seed_pages()
        status=a.run(self.pages,target="2026-11-24")
        self.assertTrue(status["ok"])
        self.assertEqual(status["quote_fresh_count"],117)
        self.assertEqual(status["official_paged_new"],116)
        self.assertTrue(status["previous_day_f18_verified"])
        self.assertEqual(status["official_paged_pages"],[1,2,3,4,5])
        data=json.loads((self.out/"quotes.json").read_text("utf8"))
        self.assertEqual(len(data["boards"]),117)
        self.assertEqual(data["boards"][0]["history_asof"],"2026-11-24")
        self.assertIsNone(data["boards"][0]["change_pct"])
        self.assertEqual(data["boards"][0]["quote_basis"],"official_bk_previous_close")
        self.assertTrue(list((self.out/"close_history").glob("*.csv")))

    def test_missing_anchor_page_does_not_fabricate_close_date(self):
        self.seed_pages(missing_last=True)
        status=a.run(self.pages,target="2026-11-24")
        self.assertFalse(status["ok"])
        self.assertEqual(status["official_paged_new"],0)
        self.assertFalse(status["previous_day_f18_verified"])
        self.assertEqual(status["quote_fresh_count"],1)
        data=json.loads((self.out/"quotes.json").read_text("utf8"))
        self.assertEqual(data["boards"][0]["history_asof"],"2026-11-20")

    def test_failed_first_attempt_uses_independent_second_attempt_page(self):
        self.seed_pages()
        original=(self.pages/"page_1.json")
        success=(self.pages/"page_1_attempt_2.json")
        success.write_bytes(original.read_bytes())
        original.unlink()
        (self.pages/"page_1_attempt_1.json").write_text(
            json.dumps({"page":1,"ok":False,"rows":[],"errors":["HTTP 502"]}),
            encoding="utf8")
        status=a.run(self.pages,target="2026-11-24")
        self.assertTrue(status["ok"])
        self.assertEqual(status["official_paged_new"],116)
        self.assertEqual(status["quote_fresh_count"],117)

    def test_mismatched_f18_anchor_does_not_replace_116_old_prices(self):
        self.seed_pages(wrong_anchor=True)
        status=a.run(self.pages,target="2026-11-24")
        self.assertFalse(status["ok"])
        self.assertEqual(status["official_paged_new"],0)
        self.assertFalse(status["previous_day_f18_verified"])
        self.assertEqual(status["quote_fresh_count"],1)

if __name__=="__main__":
    unittest.main(verbosity=2)
