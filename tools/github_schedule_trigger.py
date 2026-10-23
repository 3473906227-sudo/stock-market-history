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
from datetime import datetime
from urllib import request, error
from zoneinfo import ZoneInfo

REPO_DEFAULT = "3473906227-sudo/stock-market-history"
WORKFLOWS = {
    "stocks": "update_stocks.yml",
    "indices": "update_market_indices.yml",
    "realtime": "update_realtime_indices.yml",
}
CN = ZoneInfo("Asia/Shanghai")


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
    parser.add_argument("--mode", choices=("stocks", "indices", "realtime", "rescue"), required=True)
    parser.add_argument("--weekends", action="store_true", help="仅供测试，允许非工作日发起触发")
    args = parser.parse_args()
    now = datetime.now(CN)
    if not args.weekends and now.weekday() >= 5:
        print(f"北京时间 {now:%Y-%m-%d %H:%M:%S} 周末，跳过（节假日由采集源日期校验）")
        return 0
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
