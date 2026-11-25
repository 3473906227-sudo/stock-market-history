#!/usr/bin/env python3
"""只读检查东方财富全量BK板块列表是否可作为一次性收盘快照来源。"""
import json,time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
q=json.loads(Path("data/sectors/quotes.json").read_text(encoding="utf8"))
codes={str(x.get("code") or "") for x in q["boards"]}
print("EXPECTED_BK",len(codes),flush=True)
hosts=["https://push2.eastmoney.com/api/qt/clist/get",
       "https://push2delay.eastmoney.com/api/qt/clist/get"]
hits=set()
for fs in ("m:90+t:2+f:!50",):
  for pn in (3,):
    params={"pn":str(pn),"pz":"100","po":"1","np":"1","ut":"bd1d9ddb04089700cf9c27f6f7426281",
    "fltt":"2","invt":"2","fid":"f12","fs":fs,
    "fields":"f12,f13,f14,f2,f3,f18,f124"}
    h="https://push2.eastmoney.com/api/qt/clist/get"
    req=Request(h+"?"+urlencode(params),headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/","Accept":"application/json"})
    begin=time.monotonic()
    try:
      with urlopen(req,timeout=9) as res: raw=res.read(700001)
      d=json.loads(raw.decode("utf-8-sig"))
      data=d.get("data") or {}
      rows=data.get("diff") or []
      if isinstance(rows,dict): rows=list(rows.values())
      got={x.get("f12") for x in rows if isinstance(x,dict)}
      hits |= codes.intersection(got)
      print("PAGE",fs,pn,"total",data.get("total"),"rows",len(rows),
            "new hits",len(codes.intersection(got)),"cumulative",len(hits),
            "first",rows[0].get("f12") if rows else "-",
            "last",rows[-1].get("f12") if rows else "-",
            "time",round(time.monotonic()-begin,2),flush=True)
      if len(rows)<100:break
    except Exception as e:
      print("PAGE_FAIL",fs,pn,type(e).__name__,str(e)[:160],flush=True)
      break
    time.sleep(.4)
print("FINAL_HITS",len(hits),"/",len(codes),"missing",list(sorted(codes-hits))[:30],flush=True)
