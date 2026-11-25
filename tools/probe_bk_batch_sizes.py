#!/usr/bin/env python3
"""One-time READ-ONLY diagnostic: compare Eastmoney BK batch sizes on GitHub runner."""
import json, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from datetime import datetime
from zoneinfo import ZoneInfo
codes = []
for b in json.loads(Path("data/sectors/quotes.json").read_text(encoding="utf8"))["boards"]:
    code=str(b.get("code") or "")
    if code.startswith("BK") and b.get("history_asof")<"2026-11-24": codes.append(code)
print("CANDIDATES",len(codes), "time",datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),flush=True)
urlhosts=("https://push2.eastmoney.com/api/qt/ulist.np/get","https://push2delay.eastmoney.com/api/qt/ulist.np/get")
for n in (3,6):
    sample=(["BK0731","BK1329","BK0422"]+codes[3:n])[:n]
    params={"secids":",".join("90."+c for c in sample),"fields":"f2,f3,f18,f12,f13,f14,f124",
      "fltt":"2","invt":"2","ut":"bd1d9ddb04089700cf9c27f6f7426281"}
    for host in urlhosts:
        begin=time.monotonic()
        req=Request(host+"?"+urlencode(params), headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/","Accept":"application/json"})
        try:
            with urlopen(req,timeout=7) as res:
                payload=res.read(300001)
                status=res.status
            doc=json.loads(payload.decode("utf-8-sig"))
            rows=(doc.get("data") or {}).get("diff") or []
            if isinstance(rows,dict): rows=list(rows.values())
            valid=[x for x in rows if isinstance(x,dict) and x.get("f12") in sample and x.get("f2") not in (None,"-") and x.get("f124")]
            print("TEST",n,host.split("/")[2],status,"returned",len(rows),"valid",len(valid),"elapsed",round(time.monotonic()-begin,2),"example",json.dumps(valid[0],ensure_ascii=False)[:170] if valid else "", "YESTERDAY", [{"code":x.get("f12"),"f18":x.get("f18"),"today":x.get("f2"),"when":x.get("f124")} for x in valid[:5]],flush=True)
            break
        except Exception as e:
            print("FAIL",n,host.split("/")[2],type(e).__name__,str(e)[:120],"elapsed",round(time.monotonic()-begin,2),flush=True)
    time.sleep(.7)
