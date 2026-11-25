#!/usr/bin/env python3
"""按收盘交易日及实际完成状态判断是否需要采集。"""
from __future__ import annotations
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import os
import sys
from tools.github_schedule_trigger import a_share_trading_status

CN = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
STATUS = Path(os.environ.get("SECTOR_STATUS_FILE") or (ROOT / "data" / "sectors" / "status.json"))

def expected_close_day(now: datetime):
    day = now.astimezone(CN).date()
    if (now.astimezone(CN).hour, now.astimezone(CN).minute) < (15,20):
        day -= timedelta(days=1)
    for _ in range(20):
        result, _ = a_share_trading_status(day)
        if result == "open":
            return day.isoformat()
        if result == "unknown":
            raise ValueError("交易所休市日历尚未维护，安全跳过行业采集")
        day -= timedelta(days=1)
    raise ValueError("无法找到最近交易日")

def should_run(expected: str, old_status: dict, *, event: str, force=False, quotes_only=False):
    published = old_status.get('expected_trading_day') or ''
    if published > expected:
        return False, '已有更晚的发布批次'
    if event == 'workflow_dispatch' and force:
        return True, '手动补采'
    total = old_status.get('board_count') or 0
    quote_ok = total >= 100 and old_status.get('quote_fresh_count') == total
    history_ok = (old_status.get('ohlcv_complete') is True
                  and old_status.get('history_fresh_count') == total
                  and 'history_gaps' in old_status)
    if published == expected and quote_ok and (history_ok or quotes_only):
        return False, '该交易日所需数据已实际验收齐全'
    return True, '该交易日所需数据尚未补齐'

def main():
    try:
        day = expected_close_day(datetime.now(CN))
        try:
            status = json.loads(STATUS.read_text(encoding="utf-8"))
            if not isinstance(status,dict):
                status = {}
        except (OSError,ValueError):
            status = {}
        event = os.environ.get("GITHUB_EVENT_NAME","")
        forced = os.environ.get("SECTOR_FORCE_UPDATE","false").strip().lower() == "true"
        quotes_only = os.environ.get('SECTOR_QUOTES_ONLY', 'false') == 'true'
        run,reason = should_run(day,status,event=event,force=forced,quotes_only=quotes_only)
    except (ValueError,TypeError) as exc:
        day = ""
        run,reason = False, "安全跳过：" + str(exc)
    out = os.environ.get("GITHUB_OUTPUT","")
    if out:
        with open(out,"a",encoding="utf-8") as f:
            f.write("run_needed=" + ("true" if run else "false") + "\n")
            f.write("expected_trade_day=" + day + "\n")
    print(f"行业同步每日闸门：目标={day or '未知'}；"
          f"{'允许运行' if run else '不重复运行'}；{reason}",flush=True)
    return 0 if day else 4

if __name__ == "__main__":
    sys.exit(main())
