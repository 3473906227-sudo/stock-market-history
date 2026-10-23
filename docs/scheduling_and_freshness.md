# A股行情定时同步：防延迟配置

当前 GitHub Actions 已有错峰定时 + 补跑，仅属**尽力而为**，GitHub `schedule` 不能保证准时。推荐增加独立定时器，使用仓库自带 `tools/github_schedule_trigger.py` 通过 GitHub 官方 `workflow_dispatch` API 触发任务。

## 在 PythonAnywhere 设置（需要可用的 Scheduled Tasks 与 GitHub API 出站联网权限）

1. 从 GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens 创建仅限本仓库 `3473906227-sudo/stock-market-history` 的令牌；仓库权限授予 **Actions: Read and write**，选择短有效期，不要提交到GitHub。
2. 服务器中保存密钥到 `~/.stock_github_trigger.env`，示例为 `export STOCK_GH_DISPATCH_TOKEN='在此粘贴私密令牌'`，执行 `chmod 600 ~/.stock_github_trigger.env`。
3. 下载本仓库的 `tools/github_schedule_trigger.py` 到自己的 PythonAnywhere 项目目录（例如 `/home/用户名/github_schedule_trigger.py`），并执行手动测试：
   `bash -lc 'source ~/.stock_github_trigger.env && python3.11 ~/github_schedule_trigger.py --mode indices'`
4. 在 **Tasks → Scheduled tasks** 创建以下**北京时间**任务（若面板显示 UTC 请减8小时）：
   - 15:20 `--mode stocks`
   - 15:50 `--mode indices`
   - 16:50 `--mode rescue`（两条均补跑；如果每日任务名额有限，优先此项）
   命令结构：`bash -lc 'source ~/.stock_github_trigger.env && python3.11 ~/github_schedule_trigger.py --mode rescue'`
5. 定时器仅负责发起请求；响应204只代表 GitHub 接受触发，不保证任务立即运行。检查 Actions 结果及数据交易日期确认采集是否完成。
6. 对于 PythonAnywhere 无权创建 Scheduled Tasks 的账号，可以在自己有权限的外部服务器/计划任务上运行同一脚本，勿将令牌放在公共 cron URL、工作流、客户端 JavaScript 中。外部平台必须能够访问 api.github.com。

## 主要防护

- 个股/指数独立并发组，各自不覆盖尚未处理的其它工作流排队任务。
- 指数日线只提交指定 CSV/JSON，禁止意外覆盖 `indexes/realtime.json`。
- 实时快照 git push 前比较 `generated_at`，不允许更旧的快照覆盖更新的快照。
- 个股与指数提交具有远端竞争后自动重试功能。
- GitHub 定时器触发时间不可作为**实际行情时间**；网页应展示服务端读取时间与快照采集时间，盘中超过10分钟未更新显示预警。
- 非A股交易时间，市场价格不变化为正常；供应商没有可核验时间戳时，采集时间不等于成交时间。
