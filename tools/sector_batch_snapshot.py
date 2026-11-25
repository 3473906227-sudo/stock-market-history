#!/usr/bin/env python3
"""东方财富官方BK行业批量收盘报价。每交易日每组只请求一次；与历史OHLCV分开。

直接向 push2.eastmoney.com/api/qt/ulist.np/get 按90.BK代码批量获取，
仅接受官方BK代码、当天15:00后真实时间戳、合法指数价格及涨跌幅。
无法获取的单项保留旧快照。不会构造缺失的K线/成交量或强弱结果。
"""
from __future__ import annotations
import csv
from datetime import datetime, date, timedelta
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
from tools.github_schedule_trigger import a_share_trading_status

CN=ZoneInfo("Asia/Shanghai")
ENDPOINT="https://push2.eastmoney.com/api/qt/ulist.np/get"
DELAY_ENDPOINT="https://push2delay.eastmoney.com/api/qt/ulist.np/get"
SOURCE="东方财富官方BK行业批量收盘报价"
FIELDS="f2,f3,f18,f12,f13,f14,f124"

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

def _verified_optional_yesterday(row):
    try:
        value=float(row.get("f18"))
        return round(value,3) if math.isfinite(value) and 0<value<1000000 else None
    except (TypeError,ValueError):
        return None

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
        "previous_close":_verified_optional_yesterday(row),
        "timestamp":trade_time.strftime("%Y-%m-%d %H:%M:%S"),
    }

def _adjacent_trade_day(value, direction):
    day=date.fromisoformat(value)
    for _ in range(25):
        day+=timedelta(days=direction)
        state,_=a_share_trading_status(day)
        if state=="unknown":
            raise ValueError("缺失交易日历，不可推算昨收日期")
        if state=="open":
            return day.isoformat()
    raise ValueError("无法确认相邻交易日")

def validate_previous_close(row, codes, expected):
    """在次交易日读官方f18上一收盘，精确回补expected日close，不冒充当日涨幅。"""
    if not isinstance(row,dict):
        raise ValueError("昨日收盘数据不是对象")
    code=str(row.get("f12") or "").strip()
    if code not in codes or not re.fullmatch(r"BK\d{4,6}",code):
        raise ValueError("昨日收盘的官方BK代码不匹配")
    if str(row.get("f13"))!="90":
        raise ValueError("非东方财富官方BK市场")
    try:
        value=float(row.get("f18"))
        timestamp=int(row.get("f124"))
    except (TypeError,ValueError,OverflowError):
        raise ValueError("昨收指数或行情时间戳缺失") from None
    observed=datetime.fromtimestamp(timestamp,CN)
    if observed.date().isoformat()!=_adjacent_trade_day(expected,1):
        raise ValueError("行情不是目标收盘后的紧邻下一交易日")
    if not(math.isfinite(value) and 0<value<1000000):
        raise ValueError("昨收指数价格异常")
    return {
        "code":code,"close":round(value,3),"day":expected,
        "source_label":str(row.get("f14") or "")[:80],
        "source_time":observed.strftime("%Y-%m-%d %H:%M:%S"),
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
    # 同一次官方批量报价的f18即前交易日真实收盘价，顺带填补漏采日。
    if row.get("previous_close") and row.get("previous_close")>0:
        prev_day=_adjacent_trade_day(expected,-1)
        if prev_day not in prices:
            prices[prev_day]=row["previous_close"]
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

def apply_previous_close(board, quote, expected, history_dir, close_dir, refs):
    """仅作为被验证的前一日收盘价；没有昨日前一日参考价时当日涨跌置空。"""
    if str(board.get("history_asof") or "")>=expected:
        return False
    prices=_close_prices(board,history_dir,close_dir)
    close=quote["close"]
    if prices.get(expected) and abs(prices[expected]-close)/close>.002:
        raise ValueError("已存官方昨收价与新请求不一致")
    yesterday=_adjacent_trade_day(expected,-1)
    prev=prices.get(yesterday,0)
    def ret(days):
        base=prices.get(refs.get(days,""),0)
        return round((close/base-1)*100,3) if base>0 else None
    # 记录原始昨天价格，正日行情不可伪造OHLCV或独立涨跌。
    board.update({
        "last":close,"history_asof":expected,"history_close":close,
        "quote_asof":expected,
        "change_pct":round((close/prev-1)*100,3) if prev>0 else None,
        "ret5":ret(5),"ret20":ret(20),
        "quote_basis":"official_bk_previous_close",
        "quote_source":SOURCE + "f18昨收（次交易日核验）",
        "quote_source_url":ENDPOINT,
        "quote_source_label":quote["source_label"],
        "source_time":quote["source_time"],
        "ohlcv_complete_to_target":False,
        "history_ohlcv_asof":max(historical_prices(board["name"],history_dir).keys(),default=""),
        "ma20":None,"ma60":None,"trend":None,"volume_ratio":None,
    })
    prices[expected]=close
    _save_close_prices(board,close_dir,prices)
    return True

def update(boards, *, expected_day, history_dir, close_dir, chunk_size=6):
    """小批次递进采集，容忍单批请求失败；已有板块永远不重复抓取。

    上游批量接口对60只BK一次请求可能返回502/握手超时。默认10只一批；
    失败时仅拆成两个5只的子批次各请求一次，避免第1批失败就停止全部。
    一个交易日只有一次自动任务，单任务内请求有明确上限。
    """
    items={str(b.get("code") or ""):b for b in boards
           if re.fullmatch(r"BK\d{4,6}",str(b.get("code") or ""))}
    pending={code:b for code,b in items.items()
             if str(b.get("history_asof") or "") < expected_day}
    if not pending:
        return {"recovered":0,"attempted":0,"valid_rows":0,"failed":[],
                "requested":0,"not_fresh":[],"source_incomplete":False}
    refs=reference_days(expected_day)
    codes=sorted(pending)
    recovered=0
    valid=0
    attempts=0
    checked=set()
    errors=[]
    failed_batches=0
    stale_batches=0
    chunk_size=max(1,min(int(chunk_size),12))
    request_limit=max(12,min(45, (len(codes)+chunk_size-1)//chunk_size*3))
    def consume(part):
        nonlocal recovered,valid,attempts,failed_batches,stale_batches
        if attempts>=request_limit:
            return False
        attempts+=1
        try:
            rows=_api_rows(part)
        except Exception as exc:
            failed_batches+=1
            errors.append({"batch_size":len(part),"sample":part[:2],
                           "reason":type(exc).__name__+": "+str(exc)[:150]})
            print("OFFICIAL_BATCH_UNAVAILABLE",len(part),type(exc).__name__,
                  str(exc)[:145],flush=True)
            return False
        if not rows:
            failed_batches+=1
            errors.append({"batch_size":len(part),"reason":"官方接口返回空报价"})
            return False
        fresh_in_batch=0
        for item in rows:
            try:
                quote=validate_row(item,set(part),expected_day)
                code=quote["code"]
                if code in checked:
                    continue
                checked.add(code)
                valid+=1
                fresh_in_batch+=1
                if apply_batch_close(pending[code],quote,expected_day,history_dir,
                                     close_dir,refs):
                    recovered+=1
            except (ValueError,KeyError,TypeError,OverflowError) as exc:
                errors.append({"code":str(item.get("f12")) if isinstance(item,dict) else "unknown",
                               "reason":str(exc)[:155]})
        if fresh_in_batch==0:
            stale_batches+=1
        print("OFFICIAL_BATCH_RESULT","size",len(part),"fresh",fresh_in_batch,
              "total_valid",valid,"updated",recovered,"expected",expected_day,flush=True)
        return True

    source_fail_streak=0
    for start in range(0,len(codes),chunk_size):
        if attempts>=request_limit or source_fail_streak>=5:
            print("OFFICIAL_BATCH_CIRCUIT_BREAK",attempts,source_fail_streak,flush=True)
            break
        part=codes[start:start+chunk_size]
        ok=consume(part)
        if not ok and len(part)>=6 and attempts<request_limit:
            # 只拆一次：上游单次URL较长可能导致502；不全量重试。
            half=len(part)//2
            left=consume(part[:half])
            time.sleep(.8)
            right=consume(part[half:]) if attempts<request_limit else False
            ok=left or right
        if ok:
            source_fail_streak=0
        else:
            source_fail_streak+=1
        if start+chunk_size<len(codes):
            time.sleep(1.0)
    missing=sorted(code for code in codes if code not in checked)
    print("OFFICIAL_BATCH_COVERAGE",recovered,"/",len(codes),"requests",attempts,
          "missing",len(missing),flush=True)
    return {"recovered":recovered,"attempted":attempts,"valid_rows":valid,
            "failed":errors[:60],"requested":len(codes),"not_fresh":missing[:80],
            "missing_count":len(missing),"source_incomplete":bool(missing),
            "failure_batches":failed_batches,"stale_batches":stale_batches}
