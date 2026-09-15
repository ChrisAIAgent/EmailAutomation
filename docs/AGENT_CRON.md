# Agent Scheduling and Automation Runs

> `AGENTS.md` is authoritative for execution modes and safety. This document
> covers scheduler operations; MCP mappings are in `AGENT_CAPABILITIES.md`.

> The transition Web stack also owns TACWork Server (18002), TACWork Web (18003) and the
> OpenCode Engine. Always use `start-stack.bat` / `stop-stack.bat`; do not launch a
> second Agent stack. The formal data directory's `run/services.json` records both Email Automation and
> TACWork runtime PIDs.

业务执行由 Backend、Huey 队列和唯一 Consumer 完成。外部 Agent 或系统 Cron 只负责触发、监控和汇报，不直接修改 Gmail、数据库或 Run 状态。

## 1. 本地调度

`start-stack.bat` 会干净启动：

- FastAPI Backend
- 单个 Huey Consumer
- Next.js Frontend

Consumer 每分钟写入心跳并扫描到期 Automation。`GET /api/health` 中必须看到：

```json
{
  "status": "ok",
  "consumer": {
    "healthy": true,
    "state": "running"
  }
}
```

心跳超过 150 秒、状态不是 running 或文件不可读时，`healthy=false`。此时禁止继续排入任务。

Consumer 使用 `backend/data/consumer.lock` 的操作系统锁保证单实例。第二个 Consumer 会立即退出，不得绕过锁或删除锁文件来并行启动。

全新交接时 Huey 队列应为空，不应存在历史 queued/running Run。Agent 第一次启用 Automation 前先确认其 Campaign 与 Contacts 均由本轮真实运营创建。

首次接管只做同步、分拣和汇报，不启用 Automation。只有用户确认 Contact 和 Campaign 后，才允许建立定时跟进。

## 2. 外部触发

```powershell
powershell -ExecutionPolicy Bypass -File scripts/agent-tick.ps1
```

对应：

```text
POST /api/automation/tick
```

`tick` 只将 enabled Automation 放入 Huey 队列并立即返回。它不在 HTTP 请求中同步处理邮件。配置 `AUTOMATION_WEBHOOK_TOKEN` 时通过脚本参数提供，不得写入文档或报告。

本地 Consumer 已提供周期扫描，通常无需再创建外部每分钟 Cron。外部 Agent 更适合作为检查与汇报层。

## 3. Run 生命周期

```text
trigger -> queued -> running -> success | partial | failed
```

轮询 `GET /api/automation/{id}`，直到对应 `run_id` 进入终态。

- `queued`：尚未被 Consumer 领取。
- `running`：仍在执行。
- `success`：扫描完成，需查看 summary 和 timeline 判断实际动作。
- `partial`：部分动作失败或被阻止。
- `failed`：本次 Run 失败。

`ok=true`、`enqueued>0`、queued 或 running 都不能作为自动化成功报告。

## 4. 并发与历史幂等

- 同一 Automation 同时最多一个 `queued/running` Run。
- `already_inflight=true` 时监控现有 `run_id`，不再触发。
- 不要循环调用 tick 加速任务。
- Gmail 超时或进程重启后，先查询现有 Run、Approval 和 Gmail 结果。
- 某条入站消息已经产生 reply Approval 后，后续 Run 记录 `already_processed` 并跳过。
- 同一 thread 只有收到更新的入站消息后才重新进入回复流程。
- 新入站消息同步后会清除旧 thread 判断，下一次分拣重新计算 Contact 标签、阶段和下一步。
- `filtered`、`outbound_only/awaiting_reply` 和 `triage_review` 线程不得自动生成客户回复。
- Automation 只处理已通过真人门控和联系人准入、且已关联 Contact/Campaign 的业务线程。

## 5. 干净重启

代码更新、健康异常或 PID 不一致时：

```powershell
.\stop-stack.bat
.\start-stack.bat
```

启动脚本只清理经状态文件验证属于本应用的旧进程，绝不结束外部端口进程；过渡端口组为 18000-18003。正式 Electron 版不启动 Next Web Server。

启动后必须验证：

1. `/api/health.status=ok`
2. `/api/health.consumer.healthy=true`
3. 前端 HTTP 200
4. 正式数据目录 `run/services.json` 中只有本轮服务 PID
5. `logs/*-error.log` 无持续异常

## 6. 故障判断

- Run 长时间 queued：Consumer 离线、心跳过期或队列不可读。
- Run 长时间 running：外部依赖阻塞；检查 Gmail timeout 与 stale-run 回收。
- `TaskRegistry`：Consumer 代码或任务注册不完整，需要开发修复。
- `gmail_timeout`：Gmail 网络调用达到超时。
- `worker_timeout`：stale running Run 已回收为 failed。
- `draft_blocked`：策略阻止，等待人工决策。
- `already_processed`：历史入站消息已处理，无需重试。
- `awaiting_reply`：只有我方外发邮件，继续等待，不是待回复任务。
- `filtered`：广告、垃圾或系统邮件，不进入 Contact/Automation。
- `triage_review`：真人判断不确定，等待人工判断，不自动发送。

运营 Agent 不修改 Huey DB、不手工改 Run 状态、不删除 lock 或 status 文件。

## 7. 验收与报告

手动触发：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/agent-tick.ps1
```

生成报告：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/agent-report.ps1
```

记录 run_id、状态变化、同步线程、Filtered/真人/待复核数量、Contact 标签变化、Draft/Approval 数量、实际发送、停止数量、`draft_blocked`、`already_processed`、error、收件确认和下一次 `next_run_at`。Dashboard 的 Replies、Positive Replies、Needs Reply 必须与真人联系人工作队列一致。

## Approval reconciliation during sync

After every Gmail sync, the backend narrowly reconciles stale pending Approvals
against actual outbound messages. A match requires the same Gmail thread, the same
recipient, the current Gmail account as sender, and an identical normalized
plain-text body. A match changes the Approval to `expired`, cancels an unsent
Draft, and writes `approval_reconciled_sent` to AuditLog. Cron must report this as
reconciliation, never as a new send.

If an operator confirms that a pending Approval is already sent or must not be
sent, call `POST /api/approvals/{approval_id}/invalidate` with a reason. Do not
approve, regenerate, or directly edit SQLite to clear it. Re-read
`/api/approvals` after invalidation and report the remaining pending count.

## Inbox Gate Before Automation

Before an inbound thread can stop follow-up or create a reply Approval, it must pass the human gate. `has_human_reply=true` is valid only for a verified human campaign reply. System, advertising, spam, filtered, outbound-only, and ambiguous threads must not cancel scheduled follow-ups.

Only definite system, advertising, or spam content is automatically filtered. Forwarded messages, job applications, and scripted/automation content must remain in human review whenever sender identity, business relevance, or message intent is uncertain. A `triage_review` is not an approval for Cron/full-auto to create a Contact, Draft, Approval, reply, or send.

`job_application`, `forwarded`, and `scripted_content` remain outside Contacts at the admission gate even when a real person sent them. They must not become `needs_reply`, create an AI sales reply, or trigger autonomous follow-up. Cron/full-auto must not resolve this admission gate by silently creating a Contact.

## 8. Production Cron Contract

The external Agent Cron is an orchestration and reporting layer. It must not edit
SQLite, Huey files, OAuth files, source code, or `.env`. It may call the existing
API and scripts.

Recommended hourly job:

```text
health check -> incremental Gmail sync -> Inbox sort -> Contact/stage update

Gmail sync decodes RFC 2047 headers and MIME bodies before Inbox sort. If the normal
payload is damaged it falls back to Gmail `format=raw`; safe historical repairs emit
`gmail_mime_decode_repaired`. A decode failure must block downstream reply generation
for that message rather than allowing corrupted text into LangGraph.
-> generate tracked Approvals -> full_auto dispatch or semi_auto wait -> inspect Automation Runs -> write reports/
```

Cron and Agent Takeover may run Gmail History incremental sync only after the
user-authorized first import is complete. They must never start the full import.
They must also never start first-history Inbox triage: that fixed historical
snapshot requires a separate explicit operator authorization. Scheduled work may
only handle later Gmail History changes and their eligible untriaged threads.
If the cursor expires, stop the cycle, report the recovery requirement and wait
for an operator decision; do not silently scan a recent fixed-size page.

### Inbox versus Campaign scheduling

Inbox processing does not require an Automation or Campaign. A verified inbound
business message may produce a standalone pending Approval with `campaign_id=null`.
Do not create a dummy Campaign for it. Campaign and Huey Automation are for
outbound acquisition and scheduled follow-up.

Every scheduled Global Inbox run loads the owner's active Agent Profile before
drafting. Knowledge retrieval cannot substitute for identity/signature settings.
If Campaign context is unavailable, the run falls back to the global Profile and
audits that fallback. Signature post-processing and validation happen before a
draft becomes sendable in either execution mode.

Before sending any Approval, verify Gmail account, approval status, recipient,
suppression, sending window, idempotency, and user authorization. Never treat
queued/running/success Run status as proof that an email was delivered.

### Cloud restart checklist

After deployment or restart verify `/api/health`, `/api/gmail/status`,
`/api/system/pause`, `/api/agent-takeover`, frontend HTTP 200, TACWork/MCP
connectivity, exactly one Consumer, persistent database and Huey files, and a
current report in `reports/`. Confirm `display_timezone`, UTC `Z` timestamps,
local display timestamps, `agent_takeover_owns_global`, and the absence of a
second Global next-due entry. Use the deployment's process manager or
`start-stack.bat` only where supported; do not start a second stack.
# Agent Run mode handling

Cron-triggered automation uses the Automation's configured `execution_mode`, whose
default is `full_auto`. The Workspace-wide `approval_mode` remains the maximum authority: `human_review` forces future Agent Runs to `semi_auto`; `agent_review` permits the configured mode only after every server safety check. Neither mode may resolve an unresolved Contact-admission or content-uncertainty review. A `semi_auto` cron run prepares one frozen batch and remains
`awaiting_confirmation`; it is considered inflight so Cron must not create a second
run for that Automation. Confirmation queues the existing frozen run for dispatch;
it must not invoke planning again. Prepared plans expire after 24 hours. Include mode,
run status, confirmed-by value, sent count, blocked count, and Gmail result in every
run report.

## Scope scheduling

Cron must not enable or run a Global automation whose Plan has no
`takeover_scope`. Supported scopes are `recent_days`, `all_business`, and
`future_only`; the stored `takeover_cutoff_at` is the candidate boundary.

If a stale Run contains a Delivery Attempt in `sending`, `unknown`, or
`gmail_sent`, recovery marks it `recovery_pending` instead of retrying. Cron must
report and reconcile it. SQLite uses WAL, short per-thread commits, a 60-second
busy timeout, and bounded reclaim retries; repeated lock errors remain failures
and must not cause duplicate task creation.

The scheduler treats `scope=global` and `scope=campaign` as separate Automations.
Global Inbox automation scans the owner's eligible triaged threads. Campaign
automation retains its Campaign-only members, cadence, and limits. One module being
paused or disabled does not alter the other's schedule; the global pause remains the
only system-wide stop.

## Semi-auto Cron handoff

When a Cron-triggered Run uses `semi_auto`, Cron may enqueue preparation but must not dispatch the send. The Run remains `awaiting_confirmation` and counts as inflight, so later ticks must not create a second Run. The Agent report must contain the frozen recipient/content plan, policy blocks and expiry before requesting confirmation. After confirmation, dispatch the same Run and report Gmail message IDs and actual send results. No confirmation means no send; Drafts and queued/running states are not delivery evidence.

## Profile rules for scheduled runs

Every scheduled run loads the owner Agent Profile before drafting.
`language_policy=english` is a hard output rule for Global and Campaign runs;
knowledge language does not change it. Campaign identity override is disabled by
default and cross-owner/stale Campaign context is ignored and audited.

Scheduled preparation must enforce one pending reply Approval per Thread.
Invalidated content may be recreated only when the corrected subject/body differs;
identical retries remain blocked. Repeated Gmail 401/retry-exhausted errors stop
the run until OAuth is reconnected and a sync succeeds.

## TACWork monitoring boundary

The embedded TACWork panel can monitor and explain scheduled operations, but it is
not the scheduler and must not directly mutate Cron, Huey, SQLite, OAuth, Gmail
tokens, or Run state. It should use the same public APIs and scripts as any other
operator:

```text
scripts/agent-health.ps1
scripts/portable-health.ps1
GET /api/health
GET /api/gmail/status
GET /api/system/pause
GET /api/dashboard/readiness
GET /api/automation
GET /api/approvals?status=pending
```

For final delivery monitoring, report Email Automation service health and TACWork
panel health separately. A TACWork iframe/session problem does not prove Backend,
Consumer, Gmail, or Automation failure. Conversely, a working TACWork panel does
not prove that Gmail sync, send policy, or Consumer health is safe.

### Agent Takeover cadence, state, and local-runtime boundary

The sidebar **Agent Takeover** control schedules Global Inbox operations: incremental Gmail sync, daily Inbox triage monitoring/creation, and eligible admitted-Contact work. It never starts the first full import or first-history triage, and it never makes a Contact-admission decision. A live session capability may perform its separately authorized same-owner Campaign or Automation configuration through typed MCP tools, but it does not take ownership of Campaign's Huey cadence or bypass any policy gate.
Its cadence is a user-entered whole number of minutes from 1 through 1440.
Recommended values are 1, 15, 30, 60, 120, 240 and 1440; use 1 minute only for
short diagnostics because every due cycle creates a new TACWork root
session and correlation ID; it never inherits the earlier operating conversation.
UTC remains the stored scheduler clock. The browser persists its IANA local time
zone as the Workspace display zone; Web status, TACWork titles and reports use the
returned formatted local time with an explicit zone label.

The schedule is local, not cloud-hosted. Backend, Huey Consumer, TACWork Server,
TACWork Engine, and the computer itself must stay running. During shutdown, sleep,
or service downtime no cycle is woken; after startup, inspect
`GET /api/agent-takeover` and let the normal scheduler reconcile before taking
any manual action. Do not use `run-now` merely to compensate for an offline gap.

Monitor a cycle through `GET /api/agent-takeover`, the created TACWork root-session
snapshot, the Global Agent Run, pending Approvals, and the report. Each minute the
scheduler reconciles an idle TACWork session before it evaluates the next due time,
then writes `completed` only if every authorized MCP stage completed without error.
An idle session with a recorded MCP-stage failure is `completed_with_errors` and
retains its error for operator review. Both outcomes preserve the last Session for
history.
`current_stage=poll_agent_run:success` and a completed Agent Run mean the business
chain completed; neither `running` nor a session transcript proves that any email
was sent. Only Gmail acceptance is a sent result.

When Takeover is enabled, Global Automation is configuration owned by
`agent_takeover`, not a second Huey schedule. Do not report its persisted legacy
`next_run_at` as a next scan. Campaign Automation remains Huey-owned and is the
only Automation next-due entry shown by the Huey scheduler status.

Use `logs/mcp-server.log` with backend and Consumer logs when a scheduled session
cannot call Email Automation tools. It contains minimal MCP startup/protocol/tool
error metadata only. Telemetry failures are diagnostic and must not by themselves
block an already authorized scheduled operation. A missing or deleted prior
TACWork session is stale local state: the scheduler clears it and creates a fresh
root session in the same due cycle; other session lookup errors remain blockers
and must be reported as `previous_run_status_unknown`.

Scheduled sessions use private system context for their short-lived takeover grant;
the grant must never appear in a user-visible session prompt, transcript, report,
or MCP log. They must use registered typed `ea_*` tools only. A tool error is not a
license to use REST, Shell, source exploration, guessed endpoints, or automatic
retries: record the exact stage/error and finish safely. The scheduled status,
Dashboard, Inbox and daily-triage reads also carry that private context, so a read
failure produces `completed_with_errors` rather than a false clean completion.
`ea_takeover_status` is
the authority for takeover permissions and operating safety; `ea_dashboard` is
only the queue-metrics source. Refresh takeover status before the final report;
do not present a stale/past next-run value as a future wake-up.

### Web scheduler (no Electron dependency)

The integrated Web deployment uses the Email Automation Backend + Huey Consumer
as its scheduler. TACWork Desktop's Electron scheduled-automation IPC is not
required and must not be treated as a delivery dependency.

The Automation page uses these endpoints:

```text
GET  /api/automation/scheduler/status
POST /api/automation/{id}/schedule
POST /api/automation/scheduler/run-due
```

`run-due` performs one due-item scan. It does not bypass Automation enablement,
single-inflight idempotency, system pause, Gmail status, suppression, sending
window, daily limit, execution mode, or Approval rules. The computer and unified
services must be running; offline wake/start scheduling is a separate Windows
Task Scheduler concern for the portable installer.

When readiness reports stale `awaiting_confirmation` Runs, the monitor may
recommend cancellation or continuation. It may call
`POST /api/agent-runs/{run_id}/cancel` only when explicitly authorized for that
exact Run and only after confirming that cancellation will not send, generate
Drafts, or alter Gmail data. After cleanup, re-read readiness and pending
Approvals before reporting success.
