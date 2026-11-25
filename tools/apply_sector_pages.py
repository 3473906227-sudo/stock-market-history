#!/usr/bin/env python3
"""合并独立Runner抓取的5页东方财富BK行业行情，逐条核验后发布。

只保留仓库现有117个白名单板块，未核验日期/指数价格绝不覆盖。
对目标交易日后一个交易日f18“昨收”，必须先核实至少一个已有官方收盘锚点。
单日调度仅一次，允许本次受控一次性补采。
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from tools.sector_batch_snapshot import (
    validate_row,validate_previous_close,apply_batch_close,
    apply_previous_close,_adjacent_trade_day,CN,
)
from tools.sector_daily_gate import expected_close_day

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"data"/"sectors"
HIST=OUT/"history"
CLOSE_HISTORY=OUT/"close_history"

def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError,ValueError,TypeError):
        return {}

def atomic_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".tmp")
    tmp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+"\n",
                   encoding="utf8")
    os.replace(tmp,path)

def actual_ohlcv_date(name):
    hashname=hashlib.sha256(name.encode("utf8")).hexdigest()[:20]
    file=HIST/(hashname+".csv")
    try:
        with file.open(encoding="utf-8-sig",newline="") as handle:
            rows=csv.DictReader(handle)
            latest=""
            for row in rows:
                day=str(row.get("date") or "")[:10]
                if len(day)==10 and day>latest:
                    latest=day
            return latest
    except OSError:
        return ""

def read_pages(directory):
    records={}
    errors=[]
    received_pages=[]
    total_expected=None
    for page in range(1,6):
        # 三台独立Runner分别尝试同一页；只采纳首个返回有效JSON的副本。
        candidates=[Path(directory)/f"page_{page}_attempt_{attempt}.json"
                    for attempt in (1,2,3)]
        candidates.append(Path(directory)/f"page_{page}.json")
        payload={}
        diagnostics=[]
        for file in candidates:
            candidate=load_json(file)
            if isinstance(candidate,dict) and candidate.get("ok") and candidate.get("rows"):
                payload=candidate
                break
            if isinstance(candidate,dict) and candidate:
                diagnostics.append(str(candidate.get("errors",[]))[:90])
        if not payload:
            errors.append(f"第{page}页三次独立请求全部失败："+("; ".join(diagnostics))[:220])
            continue
        rows=payload.get("rows")
        if not isinstance(rows,list) or not (1<=len(rows)<=100):
            errors.append(f"第{page}页行情列表格式异常")
            continue
        try:
            total=int(payload.get("total"))
            if total_expected is None:
                total_expected=total
            elif total_expected!=total:
                errors.append(f"第{page}页官方列表总数变化：{total_expected}->{total}")
        except (TypeError,ValueError):
            errors.append(f"第{page}页缺少官方列表总数")
        received_pages.append(page)
        for row in rows:
            if not isinstance(row,dict):
                continue
            code=str(row.get("f12") or "")
            if code.startswith("BK"):
                records[code]=row
    return records,received_pages,errors,total_expected

def current_china_day():
    return datetime.now(CN).date().isoformat()


def anchor_is_verified(boards, rows, target):
    """必须与仓库至少一个同日官方已核验价一致才可把今天f18当昨天close。"""
    if _adjacent_trade_day(target,1)!=current_china_day():
        return False
    known=sorted(
        (b for b in boards if b.get("history_asof")==target and
         str(b.get("quote_basis") or "").startswith("official") and
         b.get("code") in rows),
        key=lambda x:(str(x.get("code"))!="BK0731",str(x.get("code"))))
    for old in known:
        try:
            got=validate_previous_close(rows[old["code"]],{old["code"]},target)
            expect=float(old.get("history_close") or old.get("last") or 0)
            if expect>0 and abs(got["close"]-expect)/expect<=.001:
                print("PAGED_BK_PREVIOUS_ANCHOR_VERIFIED",
                      old["code"],got["close"],target,flush=True)
                return True
        except (ValueError,TypeError):
            continue
    return False

def run(page_dir, *, target=None):
    expected=target or expected_close_day(datetime.now(CN))
    data=load_json(OUT/"quotes.json")
    boards=data.get("boards") if isinstance(data,dict) else None
    if not isinstance(boards,list) or len(boards)<100 or len(boards)>150:
        raise ValueError("行业看板清单异常，禁止覆盖")
    if len({str(b.get("code") or "") for b in boards})!=len(boards):
        raise ValueError("行业列表有重复BK代码")
    rows,pages,errors,total=read_pages(page_dir)
    targets={b["code"] for b in boards}
    coverage=len(targets.intersection(rows))
    print("PAGED_BK_READ",len(pages),"pages",total,"total",coverage,
          "/",len(boards),"recognized industry codes",flush=True)
    previous_mode=anchor_is_verified(boards,rows,expected)
    print("PAGED_BK_PREVIOUS_DAY_MODE",previous_mode,"expected",expected,flush=True)
    updated=0
    passed=0
    from tools.sector_daily_backup import reference_days
    refs=reference_days(expected)
    for board in boards:
        code=str(board.get("code") or "")
        if str(board.get("history_asof") or "")>=expected:
            continue
        row=rows.get(code)
        if row is None:
            continue
        try:
            try:
                quote=validate_row(row,{code},expected)
                was_previous=False
            except ValueError:
                if not previous_mode:
                    raise
                quote=validate_previous_close(row,{code},expected)
                was_previous=True
            passed+=1
            if was_previous:
                did=apply_previous_close(board,quote,expected,HIST,CLOSE_HISTORY,refs)
            else:
                did=apply_batch_close(board,quote,expected,HIST,CLOSE_HISTORY,refs)
            updated+=int(bool(did))
        except (ValueError,TypeError,KeyError,OSError) as exc:
            errors.append(code+": "+str(exc)[:140])

    fresh=sum(str(b.get("history_asof") or "")>=expected for b in boards)
    missing=[b.get("name") for b in boards if str(b.get("history_asof") or "")<expected]
    full_history=sum(actual_ohlcv_date(b["name"])>=expected for b in boards)
    finished_at=datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S")
    # 仅发布本轮已核验数据；未返回的BK仍保留以前已核验收盘日。
    if updated:
        data.update({
            "boards":boards,"fetched_at":finished_at,
            "expected_trade_date":expected,
            "source":"东方财富官方BK行业分页实时收盘/已核验次日f18昨收，逐板块保留quote_basis",
            "quote_fresh_count":fresh,"quote_pending_count":len(missing),
            "official_list_pages":pages,
            "historical_recovery_only":bool(previous_mode),
        })
        atomic_json(OUT/"quotes.json",data)
    status={
        "run_at":finished_at,"github_run_id":os.environ.get("GITHUB_RUN_ID",""),
        "expected_trading_day":expected,"mode":"full","ok":fresh==len(boards),
        "reason":(f"{fresh}/{len(boards)}个行业BK收盘核验到{expected}；"
                  f"完整OHLCV日K覆盖{full_history}/{len(boards)}；"
                  f"已验证官方分页{len(pages)}/5页；本轮新增{updated}个"),
        "board_count":len(boards),"quote_fresh_count":fresh,
        "quote_stale_count":len(missing),"quote_missing_names":missing[:117],
        "quotes_updated":bool(updated),"ohlcv_complete":full_history==len(boards),
        "history_fresh_count":full_history,
        "history_pending_count":len(boards)-full_history,
        "official_paged_new":updated,"official_paged_valid_rows":passed,
        "official_paged_pages":pages,"official_paged_total":total,
        "official_paged_errors":errors[:35],
        "previous_day_f18_verified":previous_mode,
        "selected_group_count":17,
    }
    atomic_json(OUT/"status.json",status)
    print("PAGED_BK_FINAL",status["reason"],
          "still_missing",missing[:12],flush=True)
    return status

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--page-dir",default="sector_pages")
    parser.add_argument("--expected-day",default="")
    args=parser.parse_args()
    run(Path(args.page_dir),target=args.expected_day or None)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
