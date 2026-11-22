#!/usr/bin/env python3
"""A股行业日线每日最多一次采集闸门：仅用仓库状态，不调用市场API。"""
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
STATUS = ROOT / "data" / "sectors" / "status.json"

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

def should_run(expected: str, old_status: dict, *, event: str, force=False):
    # force 仅对明确手动触发有效；schedule 不允许绕过每日上限。
    if event == "workflow_dispatch" and force:
        return True, "管理员明确强制补采"
    if (old_status.get("expected_trading_day") == expected and
            old_status.get("mode") in ("full", "quotes")):
        return False, "该交易日已有采集记录（即使源端失败也不自动重试）"
    return True, "该交易日尚无采集记录"

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
        run,reason = should_run(day,status,event=event,force=forced)
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
    return 0

if __name__ == "__main__":
    sys.exit(main())
