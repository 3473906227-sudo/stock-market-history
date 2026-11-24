#!/usr/bin/env python3
"""东方财富官方BK行业批量收盘报价。每交易日每组只请求一次；与历史OHLCV分开。

直接向 push2.eastmoney.com/api/qt/ulist.np/get 按90.BK代码批量获取，
仅接受官方BK代码、当天15:00后真实时间戳、合法指数价格及涨跌幅。
无法获取的单项保留旧快照。不会构造缺失的K线/成交量或强弱结果。
"""
from __future__ import annotations
import csv
from datetime import datetime
from pathlib import Path
import hashlib
import json
import math
import os
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from tools.sector_daily_backup import historical_prices, reference_days

CN=ZoneInfo("Asia/Shanghai")
ENDPOINT="https://push2.eastmoney.com/api/qt/ulist.np/get"
DELAY_ENDPOINT="https://push2delay.eastmoney.com/api/qt/ulist.np/get"
SOURCE="东方财富官方BK行业批量收盘报价"
FIELDS="f2,f3,f12,f13,f14,f124"

def _api_rows(codes):
    if not (1 <= len(codes) <= 60) or not all(re.fullmatch(r"BK\d{4,6}", c) for c in codes):
        raise ValueError("批量行业代码列表无效")
    params={
        "secids":",".join("90."+c for c in codes),
        "fields":FIELDS, "fltt":"2","invt":"2",
        "ut":"bd1d9ddb04089700cf9c27f6f7426281",
    }
    encoded=urlencode(params)
    errors=[]
    # 官方两条HTTPS入口各最多请求一次；仅当首入口连不上时使用延迟入口。
    # 所有返回值仍需通过真实交易日15:00、BK代码和价格校验。
    for endpoint in (ENDPOINT, DELAY_ENDPOINT):
        url=endpoint+"?"+encoded
        request=Request(url, headers={
            "User-Agent":"Mozilla/5.0 (compatible; VerifiedBKClose/1.0)",
            "Referer":"https://quote.eastmoney.com/",
            "Accept":"application/json",
        })
        try:
            with urlopen(request,timeout=12) as response:
                if response.status!=200:
                    raise IOError("东方财富批量接口HTTP "+str(response.status))
                body=response.read(750001)
            if len(body)>750000 or not body:
                raise IOError("东方财富批量接口返回空或超长响应")
            doc=json.loads(body.decode("utf-8-sig"))
            if not isinstance(doc,dict) or doc.get("rc") not in (0,"0"):
                raise ValueError("批量接口返回非成功状态")
            data=doc.get("data")
            rows=data.get("diff") if isinstance(data,dict) else None
            if isinstance(rows,dict):
                rows=list(rows.values())
            if not isinstance(rows,list):
                raise ValueError("批量接口没有行情数组")
            return rows
        except (OSError,TimeoutError,ValueError,TypeError) as exc:
            errors.append(endpoint.split("//",1)[1].split("/",1)[0]+": "+
                          type(exc).__name__+" "+str(exc)[:120])
    raise IOError("两条东方财富官方批量接口均不可用；"+ "; ".join(errors))

def validate_row(row, codes, expected):
    if not isinstance(row,dict):
        raise ValueError("行情项类型异常")
    code=str(row.get("f12") or "").strip()
    if code not in codes or not re.fullmatch(r"BK\d{4,6}",code):
        raise ValueError("BK代码不属于本批次")
    if str(row.get("f13")) != "90":
        raise ValueError("非官方90市场BK板块")
    try:
        close=float(row.get("f2"))
        pct=float(row.get("f3"))
        unix=int(float(row.get("f124")))
    except (TypeError,ValueError,OverflowError):
        raise ValueError("价格、涨跌或官方时间戳无效") from None
    if not (math.isfinite(close) and math.isfinite(pct) and 0<close<1000000 and -40<pct<40):
        raise ValueError("指数价或涨跌幅超出有效区间")
    trade_time=datetime.fromtimestamp(unix,CN)
    if trade_time.date().isoformat()!=expected or (trade_time.hour,trade_time.minute)<(15,0):
        raise ValueError("非目标交易日15:00后收盘快照："+trade_time.isoformat())
    return {
        "code":code, "close":round(close,3), "pct":round(pct,3),
        "source_label":str(row.get("f14") or "")[:80],
        "timestamp":trade_time.strftime("%Y-%m-%d %H:%M:%S"),
    }

def _history_file(board, directory):
    code=hashlib.sha256(board["name"].encode("utf-8")).hexdigest()[:20]
    return Path(directory)/(code+".csv")

def _close_prices(board, history_dir, close_dir):
    values={}
    file=_history_file(board,close_dir)
    try:
        with file.open(encoding="utf-8-sig",newline="") as f:
            for row in csv.DictReader(f):
                try:
                    d=row["date"].strip()[:10];v=float(row["close"])
                    if re.fullmatch(r"\d{4}-\d\d-\d\d",d) and v>0:
                        values[d]=v
                except (KeyError,TypeError,ValueError,AttributeError):
                    continue
    except OSError:
        pass
    # 官方完整日K更可信，若日期相同优先使用完整历史。
    values.update(historical_prices(board["name"],history_dir))
    # 仓库过去已核验过的快照可作为该日真实收盘价单点，绝不虚构中间日。
    d=str(board.get("history_asof") or "")
    try:
        v=float(board.get("history_close") or board.get("last") or 0)
        if re.fullmatch(r"\d{4}-\d\d-\d\d",d) and v>0:
            values[d]=v
    except (TypeError,ValueError):
        pass
    return values

def _save_close_prices(board, close_dir, values):
    dest=_history_file(board,close_dir)
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=dest.with_suffix(".csv.tmp")
    with tmp.open("w",encoding="utf-8",newline="") as f:
        writer=csv.writer(f)
        writer.writerow(["date","close"])
        for d in sorted(values):
            if re.fullmatch(r"\d{4}-\d\d-\d\d",d):
                writer.writerow([d,round(values[d],3)])
    os.replace(tmp,dest)

def apply_batch_close(board, row, expected, history_dir, close_dir, refs):
    if str(board.get("history_asof") or "")>=expected:
        return False
    prices=_close_prices(board,history_dir,close_dir)
    def ret(offset):
        value=prices.get(refs.get(offset,""),0)
        return round((row["close"]/value-1)*100,3) if value>0 else None
    prior=prices.get(expected)
    # 当前日旧官方日K若存在可校验一致性；不允许以相差显著的截面价格覆盖。
    if prior is not None and abs(prior-row["close"])/max(prior,1) > .002:
        raise ValueError("已存完整行情与批量收盘差异过大，保留原数据")
    board.update({
        "last":row["close"], "change_pct":row["pct"],
        "history_asof":expected, "history_close":row["close"], "quote_asof":expected,
        "ret5":ret(5), "ret20":ret(20),
        "quote_basis":"official_bk_batch_close",
        "quote_source":SOURCE, "quote_source_url":ENDPOINT,
        "quote_source_label":row["source_label"], "source_time":row["timestamp"],
        "history_ohlcv_asof":max(historical_prices(board["name"],history_dir).keys(),default=""),
        "ohlcv_complete_to_target":False,
        "ma20":None, "ma60":None, "trend":None, "volume_ratio":None,
    })
    prices[expected]=row["close"]
    _save_close_prices(board,close_dir,prices)
    return True

def update(boards, *, expected_day, history_dir, close_dir, chunk_size=60):
    items={str(b.get("code") or ""):b for b in boards
           if re.fullmatch(r"BK\d{4,6}",str(b.get("code") or ""))}
    pending={code:b for code,b in items.items() if str(b.get("history_asof") or "")<expected_day}
    if not pending:
        return {"recovered":0,"attempted":0,"valid_rows":0,"failed":[]}
    refs=reference_days(expected_day)
    codes=sorted(pending)
    seen=set()
    recovered=0
    valid=0
    failed=[]
    attempts=0
    chunk_size=max(1,min(int(chunk_size),60))
    for start in range(0,len(codes),chunk_size):
        part=codes[start:start+chunk_size]
        attempts+=1
        try:
            rows=_api_rows(part)
        except Exception as exc:
            failed.append({"batch":len(part),"error":type(exc).__name__+": "+str(exc)[:160]})
            print("OFFICIAL_BATCH_UNAVAILABLE",len(part),type(exc).__name__,str(exc)[:130],flush=True)
            # 当整个官方接口被限流时避免对其余批次连续撞限流。
            if recovered==0 and attempts==1:
                break
            continue
        for item in rows:
            try:
                q=validate_row(item,set(part),expected_day)
                code=q["code"]
                if code in seen:continue
                seen.add(code)
                valid+=1
                if apply_batch_close(pending[code],q,expected_day,history_dir,close_dir,refs):
                    recovered+=1
            except (ValueError,KeyError,TypeError) as exc:
                failed.append({"code":str(item.get("f12")) if isinstance(item,dict) else "unknown",
                               "error":str(exc)[:150]})
        print("OFFICIAL_BATCH_RESULT",len(part),"valid",valid,"updated",recovered,
              "day",expected_day,flush=True)
        if start+chunk_size<len(codes):
            time.sleep(1.6)
    return {"recovered":recovered,"attempted":attempts,"valid_rows":valid,
            "failed":failed[:40],"requested":len(codes)}
