#!/usr/bin/env python3
"""仅补齐2026-11-20真实板块收盘快照（一次性应急修复）。

取得的是VerityRank 15:10发布并注明来源为东方财富的BK代码收盘指数，
不是直连东方财富官方API，也不是完整OHLCV。只更新验证过的板块及
按真实前5/20交易日已落盘收盘价计算的区间涨跌，不制造缺失K线。
"""
from __future__ import annotations

import csv
import gzip
from datetime import date, timedelta, datetime, timezone
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
    # 页面展示名可能有别名，BK代码作为官方指数唯一身份；保留原页面标签供审计。
    heading=r"行业板块\s*\|\s*·\s*\|\s*"+re.escape(code)+r"\s*\|\s*· CN\s*\|\s*([^|]{1,48})\s*\|"
    source_name=re.search(heading,visible)
    if not source_name:
        raise ValueError("BK代码与第三方页面身份无法核验")
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
    return {"close":close,"change_pct":change,"trade_date":trade_day,
            "source_time":trade_day+" "+clock,"source_label":source_name.group(1).strip()}

def get_page(code):
    request=Request(SOURCE+code.lower(), headers={
        "User-Agent":"Mozilla/5.0 (compatible; AShareVerifiedCloseRepair/1.0)",
        "Accept-Language":"zh-CN,zh;q=0.9","Accept":"text/html"
    })
    with urlopen(request, timeout=22) as response:
        if response.status!=200:
            raise IOError("HTTP "+str(response.status))
        raw=response.read(1500001)
    if len(raw)>1500000 or len(raw)<2000:
        raise IOError("异常或空的网页快照")
    if raw.startswith(bytes.fromhex("1f8b")):
        # 部分BK板块返回未标注的gzip正文（此前当作乱码而跳过）。
        try:
            raw=gzip.decompress(raw)
        except (OSError,ValueError):
            raise IOError("备用源gzip网页损坏，拒绝写入") from None
    if len(raw)>1500000 or len(raw)<2000:
        raise IOError("解压后行业页面大小异常")
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


def _apply_quote(item, *, close, pct, past, reference, source, basis,
                 source_url="", source_time="", full_history=False):
    item.update({
        "last":round(close,3),
        "change_pct":round(pct,3) if pct is not None else None,
        "history_asof":TARGET,
        "history_close":round(close,3),
        "quote_asof":TARGET,
        "history_ohlcv_asof":TARGET if full_history else (max(past.keys()) if past else ""),
        "ret5":round((close/past[reference[5]]-1)*100,3)
               if past.get(reference[5],0)>0 else None,
        "ret20":round((close/past[reference[20]]-1)*100,3)
                if past.get(reference[20],0)>0 else None,
        "quote_basis":basis,"quote_source":source,
        "quote_source_url":source_url,
        "source_time":source_time,
        "ohlcv_complete_to_target":bool(full_history),
    })
    if full_history:
        series=[past[d] for d in sorted(past) if d<=TARGET]
        if len(series)>=60:
            ma20=sum(series[-20:])/20
            ma60=sum(series[-60:])/60
            item["ma20"]=round(ma20,3)
            item["ma60"]=round(ma60,3)
            if close>=ma20>=ma60:
                item["trend"]="bullish"
            elif close<=ma20<=ma60:
                item["trend"]="bearish"
            elif close>=ma20:
                item["trend"]="above_ma20"
            else:
                item["trend"]="below_ma20"
    else:
        # 备用源只有收盘指数，不具备可核验的当日成交量和完整日K。
        for key in ("ma20","ma60","trend","volume_ratio"):
            item[key]=None

def main():
    payload=json.loads(QUOTES.read_text("utf-8"))
    boards=payload.get("boards",[])
    if not isinstance(boards,list) or not (90<=len(boards)<=150):
        raise ValueError("行业白名单快照异常，拒绝批量覆盖")
    reference=reference_dates(TARGET,back_counts=(1,5,20))
    print("基准交易日：",reference,flush=True)
    previously_done=0
    official_updated=[]
    mirror_updated=[]
    failed=[]
    network_fail_streak=0
    network_requests=0
    for item in boards:
        name=str(item.get("name") or "").strip()
        code=str(item.get("code") or "").strip()
        if item.get("history_asof")==TARGET:
            previously_done+=1
            continue
        past=historical_reference(name)
        try:
            # 缺少旧的前一交易日收盘时，第三方当日涨跌幅仍可独立核验。
            if TARGET in past and past[TARGET]>0:
                # 优先修复已有的官方真实完整日K，不访问任何第三方网站。
                close=past[TARGET]
                change_pct=(close/past[reference[1]]-1)*100 if past.get(reference[1],0)>0 else None
                _apply_quote(item,close=close,pct=change_pct,past=past,
                             reference=reference,full_history=True,
                             basis="official_daily_close",
                             source="GitHub仓库已有东方财富官方历史完整收盘日K",
                             source_time=TARGET+" 15:00")
                official_updated.append({"name":name,"code":code,"close":close})
                print("OFFICIAL_RECOVERED",name,code,TARGET,close,flush=True)
                continue

            # 只对尚未取得11月20日的板块各请求一次，不重复抓已更新80个。
            network_requests+=1
            raw=get_page(code)
            data=parse_verified_html(raw,name,code)
            close=data["close"]
            if max(past.keys())>=TARGET:
                raise ValueError("仓库已有更新的官方完整日K，禁止第三方覆盖")
            _apply_quote(item,close=close,pct=data["change_pct"],past=past,
                         reference=reference,full_history=False,
                         basis="third_party_verified_eastmoney_BK_close",
                         source=SOURCE_LABEL,source_url=SOURCE+code.lower(),
                         source_time=data["source_time"])
            item["quote_source_label"]=data["source_label"]
            mirror_updated.append({"name":name,"code":code,"close":close,
                                   "source_label":data["source_label"],
                                   "ret5_available":item["ret5"] is not None,
                                   "ret20_available":item["ret20"] is not None})
            network_fail_streak=0
            print("MIRROR_RECOVERED",name,code,data["source_time"],close,flush=True)
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,KeyError,TypeError) as exc:
            reason=type(exc).__name__+": "+str(exc)[:160]
            failed.append({"name":name,"code":code,"reason":reason})
            print("UNAVAILABLE",name,code,reason,flush=True)
            if isinstance(exc,(HTTPError,URLError,TimeoutError,OSError)):
                network_fail_streak+=1
            else:
                network_fail_streak=0
            if network_fail_streak>=5:
                print("备用网站连续5次网络故障，本轮停止请求",flush=True)
                break
        if network_requests:
            time.sleep(1.1)

    updated_count=len(official_updated)+len(mirror_updated)
    current_fresh=sum(x.get("history_asof")==TARGET for x in boards)
    if updated_count:
        payload["boards"]=boards
        payload["fetched_at"]=datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        payload["quote_recovery_target_day"]=TARGET
        payload["quote_recovery_source"]="官方历史日K优先；备用为"+SOURCE_LABEL
        payload["quote_recovery_note"]="真实1/5/20日收盘价基准计算，第三方快照不虚构OHLCV"
        atomic_json(QUOTES,payload)
    status={
        "target_date":TARGET,"requested_count":len(boards),
        "previously_fresh_count":previously_done,
        "official_recovered_count":len(official_updated),
        "mirror_recovered_count":len(mirror_updated),
        "recovered_count":updated_count,
        "fresh_total":current_fresh,
        "remaining_count":len(boards)-current_fresh,
        "unavailable_count":len(failed),
        "full_coverage":current_fresh==len(boards),
        "source":"优先官方已存日K；其余"+SOURCE_LABEL,
        "reference_dates":reference,
        "official_sample":official_updated[:15],
        "mirror_sample":mirror_updated[:15],
        "failed":failed[:60],
        "network_requests":network_requests,
        "missing_ret5_count":sum(x.get("history_asof")==TARGET and x.get("ret5") is None for x in boards),
        "missing_ret20_count":sum(x.get("history_asof")==TARGET and x.get("ret20") is None for x in boards),
        "note":"真实11月20日收盘/1日/5日/20日变化；备用源无完整OHLCV则标记缺失"
    }
    atomic_json(STATUS,status)
    print("RECOVERY_SUMMARY",json.dumps({k:v for k,v in status.items()
              if k not in ("official_sample","mirror_sample","failed")},ensure_ascii=False),flush=True)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
