#!/usr/bin/env python3
"""供 PythonAnywhere/其它独立调度器触发 GitHub 工作流；不保存密钥，不采集股票。
用法: STOCK_GH_DISPATCH_TOKEN=... python3 github_schedule_trigger.py --mode stocks
      python3 github_schedule_trigger.py --mode indices
      python3 github_schedule_trigger.py --mode rescue
通过个人访问令牌（Fine-grained PAT）授予此仓库 Actions: Read and write。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from urllib import request, error
from zoneinfo import ZoneInfo

REPO_DEFAULT = "3473906227-sudo/stock-market-history"
WORKFLOWS = {
    "stocks": "update_stocks.yml",
    "indices": "update_market_indices.yml",
    "realtime": "update_realtime_indices.yml",
}
CN = ZoneInfo("Asia/Shanghai")

# 上交所2026年官方休市安排（2025-12-22发布）：
# https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml
# 未核实年度日历不能假设工作日就是交易日，避免节假日无效抓取与付费API调用。
A_SHARE_CLOSED_RANGES = {
    2026: (
        ("2026-01-01", "2026-01-03"), # 元旦
        ("2026-02-15", "2026-02-23"), # 春节
        ("2026-04-04", "2026-04-06"), # 清明
        ("2026-05-01", "2026-05-05"), # 劳动节
        ("2026-06-19", "2026-06-21"), # 端午
        ("2026-09-25", "2026-09-27"), # 中秋
        ("2026-10-01", "2026-10-07"), # 国庆
    ),
}


def a_share_trading_status(day):
    """返回open/closed/unknown及说明，使用北京时间A股交易日历。"""
    if day.weekday() >= 5:
        return "closed", "周六、周日休市"
    closed = A_SHARE_CLOSED_RANGES.get(day.year)
    if closed is None:
        return "unknown", f"{day.year}年休市日历尚未录入，请按交易所新年度公告更新脚本"
    value = day.isoformat()
    if any(start <= value <= end for start, end in closed):
        return "closed", "官方节假日休市"
    return "open", "A股正常交易日"


def check_trading_day(day):
    status, reason = a_share_trading_status(day)
    print(f"北京时间 {day}: {status}；{reason}", flush=True)
    if status == "unknown":
        print("::error::交易日历年份未配置，安全停止自动采集，需更新休市数据。",file=sys.stderr)
        return 4
    outputs = os.environ.get("GITHUB_OUTPUT","")
    if outputs:
        with open(outputs,"a",encoding="utf-8") as f:
            f.write(f"trading_day={'true' if status == 'open' else 'false'}\n")
    return 0


def dispatch(name: str, repo: str, token: str) -> None:
    workflow = WORKFLOWS[name]
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    body = json.dumps({"ref": "main"}).encode("utf-8")
    req = request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "stock-market-history-external-cron/1.0",
        "Content-Type": "application/json",
    })
    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=16) as response:
                if response.status != 204:
                    raise RuntimeError(f"GitHub HTTP {response.status}")
            print(f"已提交执行请求: {name} ({workflow})；注意 204 仅代表接收请求，不代表采集成功", flush=True)
            return
        except error.HTTPError as exc:
            # 未授权/权限不足/不存在等错误无法靠重试恢复。不要输出令牌。
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"GitHub 工作流触发失败: {workflow} HTTP {exc.code}") from None
        except (error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"GitHub 工作流触发失败: {workflow} 网络错误: {type(exc).__name__}") from None
        time.sleep(2 * (attempt + 1))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("stocks", "indices", "realtime", "rescue"))
    parser.add_argument("--check-trading-day", action="store_true",
                        help="GitHub Actions前置交易日检查")
    parser.add_argument("--date", default="",
                        help="测试时指定YYYY-MM-DD，仅适用于--check-trading-day")
    args = parser.parse_args()
    now = datetime.now(CN)
    try:
        day = date.fromisoformat(args.date) if args.date else now.date()
    except ValueError:
        parser.error("--date格式必须为YYYY-MM-DD")
    status, reason = a_share_trading_status(day)
    if args.check_trading_day:
        return check_trading_day(day)
    if args.date:
        parser.error("--date仅用于--check-trading-day")
    if not args.mode:
        parser.error("请指定--mode")
    if status == "closed":
        print(f"北京时间 {day}：{reason}；非交易日跳过，不请求GitHub API。")
        return 0
    if status == "unknown":
        print(f"北京时间 {day}：{reason}；拒绝盲目触发任务。",file=sys.stderr)
        return 4
    repo = os.environ.get("STOCK_GH_REPO", REPO_DEFAULT).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise SystemExit("STOCK_GH_REPO 不符合 owner/repo 格式")
    token = os.environ.get("STOCK_GH_DISPATCH_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 STOCK_GH_DISPATCH_TOKEN：请在服务器私密环境变量设置，禁止写进仓库。")
    modes = ("stocks", "indices") if args.mode == "rescue" else (args.mode,)
    print(f"独立触发器启动 北京时间 {now:%Y-%m-%d %H:%M:%S}，模式 {args.mode}")
    errors = []
    for mode in modes:
        try:
            dispatch(mode, repo, token)
        except RuntimeError as exc:
            errors.append(str(exc))
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
