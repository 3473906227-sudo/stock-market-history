#!/usr/bin/env python3
"""单次联网验证东方财富多BK代码批量收盘报价是否可用，不写行情。"""
import json
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

codes=("BK1329","BK0422","BK0731")
url="https://push2.eastmoney.com/api/qt/ulist.np/get?"+urlencode({
    "secids":",".join("90."+x for x in codes),
    "fields":"f2,f3,f12,f13,f14,f124,f127,f128",
    "fltt":"2",
    "ut":"bd1d9ddb04089700cf9c27f6f7426281",
    "invt":"2",
})
print("北京时间:",datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
print("接口类别：东方财富BK批量收盘行情，BK代码数:",len(codes),flush=True)
try:
    req=Request(url,headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/",
                              "Accept":"application/json"})
    with urlopen(req,timeout=12) as r:
        print("HTTP",r.status,flush=True)
        body=r.read(250001)
    if len(body)>250000:raise ValueError("响应过大")
    data=json.loads(body.decode("utf-8-sig"))
    print("TOP_KEYS",list(data.keys()))
    core=data.get("data")
    print("DATA_KEYS",list(core.keys()) if isinstance(core,dict) else None)
    rows=(core or {}).get("diff",[]) if isinstance(core,dict) else []
    print("DIFF_TYPE",type(rows).__name__,"COUNT",len(rows) if hasattr(rows,'__len__') else "NA")
    if isinstance(rows,dict):rows=list(rows.values())
    for row in rows[:3]:
        print("ROW",json.dumps(row,ensure_ascii=False)[:650])
except Exception as e:
    print("BATCH_PROBE_ERROR",type(e).__name__,str(e)[:200],flush=True)
