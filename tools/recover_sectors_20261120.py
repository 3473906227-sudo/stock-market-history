#!/usr/bin/env python3
"""仅补齐2026-11-20真实板块收盘快照（一次性应急修复）。

取得的是VerityRank 15:10发布并注明来源为东方财富的BK代码收盘指数，
不是直连东方财富官方API，也不是完整OHLCV。只更新验证过的板块及
按真实前5/20交易日已落盘收盘价计算的区间涨跌，不制造缺失K线。
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from tools.github_schedule_trigger import a_share_trading_status

ROOT=Path(__file__).resolve().parents[1]
SECTORS=ROOT/"data"/"sectors"
QUOTES=SECTORS/"quotes.json"
STATUS=SECTORS/"recovery_20261120_status.json"
TARGET="2026-11-20"
SOURCE="https://www.verityrank.com.cn/markets/sectors/"
SOURCE_LABEL="维瑞评级转引东方财富行业BK板块2026-11-20收盘快照（非官方直接API）"

class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bits=[]
        self.block=0
    def handle_starttag(self,tag,attrs):
        if tag in ("script","style"):
            self.block+=1
    def handle_endtag(self,tag):
        if tag in ("script","style"):
            self.block=max(0,self.block-1)
    def handle_data(self,data):
        if self.block==0 and data.strip():
            self.bits.append(data.strip())

def parse_verified_html(raw, name, code):
    h=VisibleText()
    h.feed(raw)
    visible=" | ".join(h.bits)
    if not re.fullmatch(r"BK\d{4,6}",code):
        raise ValueError("板块代码非可信BK代码")
    heading=r"行业板块\s*\|\s*·\s*\|\s*"+re.escape(code)+r"\s*\|\s*· CN\s*\|\s*"+re.escape(name)+r"\s*\|"
    if not re.search(heading,visible):
        raise ValueError("BK代码与行业名称无法一一验证")
    pattern=(r"沪深 A 股行业板块与概念板块，交易时段每 5 分钟刷新，展示涨跌幅、成交额、涨跌家数与领涨股。"
             r"\s*\|\s*([\d,]+(?:\.\d+)?)\s*\|\s*([+-]?\d+(?:\.\d+)?)%\s*\|"
             r"\s*更新于\s*\|\s*(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2})")
    match=re.search(pattern,visible)
    if not match:
        raise ValueError("未找到板块收盘指数、涨跌幅和来源日期")
    close=float(match.group(1).replace(",",""))
    change=float(match.group(2))
    trade_day=match.group(3)
    clock=match.group(4)
    if trade_day!=TARGET or clock<"15:00" or not (0<close<1000000) or not (-40<change<40):
        raise ValueError("板块收盘数据不是11月20日15:00之后可信收盘快照")
    close_match=re.search(r"收盘\s*\|\s*([\d,]+(?:\.\d+)?)",visible)
    if not close_match or abs(float(close_match.group(1).replace(",",""))-close)>0.011:
        raise ValueError("页面收盘数值与标识不一致")
    return {"close":close,"change_pct":change,"trade_date":trade_day,"source_time":trade_day+" "+clock}

def get_page(code):
    request=Request(SOURCE+code.lower(), headers={
        "User-Agent":"Mozilla/5.0 (compatible; AShareVerifiedCloseRepair/1.0)",
        "Accept-Language":"zh-CN,zh;q=0.9","Accept":"text/html"
    })
    with urlopen(request, timeout=13) as response:
        if response.status!=200:
            raise IOError("HTTP "+str(response.status))
        raw=response.read(1500001)
    if len(raw)>1500000 or len(raw)<2000:
        raise IOError("异常或空的网页快照")
    return raw.decode("utf-8","replace")

def reference_dates(target,back_counts=(5,20)):
    current=date.fromisoformat(target)
    out={}
    found=0
    for _ in range(90):
        current -= timedelta(days=1)
        state,_=a_share_trading_status(current)
        if state=="unknown":
            raise ValueError("交易日历不完整，禁止猜测5日/20日基准价")
        if state=="open":
            found+=1
            if found in back_counts:
                out[found]=current.isoformat()
            if found>=max(back_counts):
                return out
    raise ValueError("无法取得20个交易日前的真实日期")

def historical_reference(name):
    file=SECTORS/"history"/(hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]+".csv")
    result={}
    try:
        with file.open("r",encoding="utf-8-sig",newline="") as stream:
            for row in csv.DictReader(stream):
                try:
                    day=str(row.get("date") or "").strip()[:10]
                    close=float(row["close"])
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",day) and close>0:
                        result[day]=close
                except (ValueError,TypeError,KeyError):
                    continue
    except OSError:
        pass
    return result

def atomic_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    os.replace(tmp,path)

def main():
    payload=json.loads(QUOTES.read_text("utf-8"))
    boards=payload.get("boards",[])
    if not isinstance(boards,list) or not (90<=len(boards)<=150):
        raise ValueError("板块数量不符合已核实的117个白名单，不执行应急补采")
    reference=reference_dates(TARGET)
    print("验证日期，5日基准",reference[5],"20日基准",reference[20],flush=True)
    recovered=[]
    failed=[]
    consecutive=0
    untouched=0
    for item in boards:
        name=str(item.get("name") or "").strip()
        code=str(item.get("code") or "").strip()
        if item.get("history_asof")==TARGET and item.get("quote_basis")=="official_daily_close":
            untouched+=1
            continue
        try:
            raw=get_page(code)
            data=parse_verified_html(raw,name,code)
            past=historical_reference(name)
            if reference[5] not in past or reference[20] not in past:
                raise ValueError("未找到实际5日或20日前收盘价，不能冒充最新区间收益")
            previous_history=max(past.keys()) if past else ""
            close=data["close"]
            if not previous_history or previous_history>=TARGET:
                # 保护已经有较新官方完整OHLCV的行情，不以备用源覆盖。
                raise ValueError("已有更新完整的行业历史，不覆盖")
            item.update({
                "last":round(close,3),
                "change_pct":round(data["change_pct"],3),
                "history_asof":TARGET,
                "history_close":round(close,3),
                "quote_asof":TARGET,
                "history_ohlcv_asof":previous_history,
                "ret5":round((close/past[reference[5]]-1)*100,3),
                "ret20":round((close/past[reference[20]]-1)*100,3),
                "ma20":None,"ma60":None,"trend":None,"volume_ratio":None,
                "quote_basis":"third_party_verified_eastmoney_BK_close",
                "quote_source":SOURCE_LABEL,
                "quote_source_url":SOURCE+code.lower(),
                "source_time":data["source_time"],
                "ohlcv_complete_to_target":False,
            })
            recovered.append({"name":name,"code":code,"close":close,"pct":data["change_pct"]})
            consecutive=0
            print("VERIFIED",len(recovered),name,code,data["source_time"],
                  "close",close,"change%",data["change_pct"],flush=True)
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,KeyError,TypeError) as exc:
            msg=type(exc).__name__+": "+str(exc)[:150]
            failed.append({"name":name,"code":code,"reason":msg})
            consecutive+=1
            print("UNAVAILABLE",name,code,msg,flush=True)
            if consecutive>=5:
                print("停止继续访问备用源：连续5次失败，保护行情网站",flush=True)
                break
        time.sleep(0.95)
    if recovered:
        payload["boards"]=boards
        payload["fetched_at"]="2026-11-22 12:20:00"
        payload["quote_recovery_target_day"]=TARGET
        payload["quote_recovery_source"]=SOURCE_LABEL
        payload["quote_recovery_note"]="当日与5/20日为真实收盘价和实存基准收盘价计算；日K OHLCV仍需单独补全"
        atomic_json(QUOTES,payload)
    status={
        "target_date":TARGET,"requested_count":len(boards),"recovered_count":len(recovered),
        "already_official_count":untouched,"unavailable_count":len(failed),
        "full_coverage":len(recovered)+untouched==len(boards),
        "source":SOURCE_LABEL, "reference_dates":reference,
        "verified":recovered[:15],"failed":failed[:50],
        "note":"本次恢复收盘指数与1/5/20日涨跌；历史OHLCV未补齐，不用于伪造K线"
    }
    atomic_json(STATUS,status)
    print("RECOVERY_SUMMARY",json.dumps({k:v for k,v in status.items()
              if k not in ("verified","failed")},ensure_ascii=False),flush=True)
    # 部分恢复也可安全发布；任务是否完整由状态文件判断。
    return 0 if recovered or untouched else 2

if __name__=="__main__":
    raise SystemExit(main())
