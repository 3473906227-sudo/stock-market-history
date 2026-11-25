#!/usr/bin/env python3
"""在独立GitHub Runner上读取一页官方BK行业索引，带时间戳与失败证据。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from datetime import datetime
from zoneinfo import ZoneInfo

HOSTS=(
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
)
CN=ZoneInfo("Asia/Shanghai")

def fetch_page(page:int):
    if not 1<=page<=8:
        raise ValueError("页码范围无效")
    params={
        "pn":str(page),"pz":"100","po":"1","np":"1",
        "ut":"bd1d9ddb04089700cf9c27f6f7426281",
        "fltt":"2","invt":"2","fid":"f12",
        "fs":"m:90+t:2+f:!50",
        "fields":"f12,f13,f14,f2,f3,f18,f124",
    }
    errors=[]
    for host in HOSTS:
        try:
            req=Request(host+"?"+urlencode(params),headers={
                "User-Agent":"Mozilla/5.0 (compatible; VerifiedBKDaily/1.0)",
                "Referer":"https://quote.eastmoney.com/",
                "Accept":"application/json",
            })
            with urlopen(req,timeout=13) as response:
                if response.status!=200:
                    raise IOError("HTTP "+str(response.status))
                body=response.read(950001)
            if not body or len(body)>950000:
                raise IOError("行业页返回空或过大")
            doc=json.loads(body.decode("utf-8-sig"))
            if not isinstance(doc,dict) or doc.get("rc") not in (0,"0"):
                raise ValueError("官方BK行情结果状态异常")
            data=doc.get("data")
            if not isinstance(data,dict):
                raise ValueError("官方BK返回数据结构错误")
            rows=data.get("diff") or []
            if isinstance(rows,dict):
                rows=list(rows.values())
            if not isinstance(rows,list) or not rows or len(rows)>100:
                raise ValueError("官方BK分页数据为空或长度异常")
            rows=[x for x in rows if isinstance(x,dict)]
            return {
                "page":page,"ok":True,"total":data.get("total"),
                "rows":rows,
                "received_at":datetime.now(CN).isoformat(),
                "endpoint":host,"errors":errors,
            }
        except Exception as exc:
            errors.append(type(exc).__name__+": "+str(exc)[:150])
    return {"page":page,"ok":False,"rows":[],"errors":errors,
            "received_at":datetime.now(CN).isoformat()}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--page",type=int,required=True)
    parser.add_argument("--output-dir",default="sector_pages")
    args=parser.parse_args()
    result=fetch_page(args.page)
    path=Path(args.output_dir)
    path.mkdir(parents=True,exist_ok=True)
    (path/f"page_{args.page}.json").write_text(
        json.dumps(result,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    print("BK_PAGE_FETCH",args.page,"ok",result["ok"],
          "rows",len(result["rows"]),"total",result.get("total"),
          "errors",result["errors"][:2],flush=True)
    # 将失败状态写入artifact而非阻止后续已通过校验的数据发布。
    return 0

if __name__=="__main__":
    raise SystemExit(main())
