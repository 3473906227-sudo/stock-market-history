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
for fs in ("m:90+t:2+f:!50","m:90+t:3+f:!50"):
  for h in hosts:
    params={"pn":"1","pz":"1800","po":"1","np":"1","ut":"bd1d9ddb04089700cf9c27f6f7426281",
    "fltt":"2","invt":"2","fid":"f3","fs":fs,
    "fields":"f12,f13,f14,f2,f3,f18,f124"}
    req=Request(h+"?"+urlencode(params),headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/","Accept":"application/json"})
    t=time.monotonic()
    try:
      with urlopen(req,timeout=8) as r:
        body=r.read(1200000)
        st=r.status
      d=json.loads(body.decode("utf-8-sig"))
      rows=(d.get("data") or {}).get("diff") or []
      if isinstance(rows,dict):rows=list(rows.values())
      got={x.get("f12") for x in rows if isinstance(x,dict)}
      hit=codes.intersection(got)
      print("CLIST_OK",fs,h.split("/")[2],"http",st,"count",len(rows),"matching",len(hit),
      "sec",round(time.monotonic()-t,2),"sample",json.dumps(rows[:2],ensure_ascii=False)[:410],
      "missing",list(sorted(codes-hit))[:8],flush=True)
      break
    except Exception as e:
      print("CLIST_FAIL",fs,h.split("/")[2],type(e).__name__,str(e)[:180],
      round(time.monotonic()-t,2),flush=True)
