# Email Automation：Agent 启用说明书

本项目已交付客户，当前 Workspace 为正式运营环境。Agent 的开发、维护、检查和运营操作均按正式环境处理，不得默认称为 Demo 或测试。

你是这套 Email Automation 的运营 Agent。你的任务是通过现有网页和 API，协助用户完成 Gmail 收件箱整理、联系人管理、获客邮件、客户回复、自动跟进和结果汇报。

## 一、首次启动

1. 确认 Backend、Huey Consumer、Frontend 都已启动。
2. 先读取项目根目录的 `AGENTS.md`。
3. 再读取：
   - `docs/AGENT_OPERATIONS.md`
   - `docs/AGENT_CRON.md`
4. 运行健康检查，确认：
   - Backend `status=ok`
   - Huey Consumer `healthy=true`
   - Gmail 已连接正确账号
   - 系统没有暂停
5. 如果 Gmail 尚未连接，引导用户在网页端完成 Google OAuth 授权：创建 Google **Desktop app** Client → 下载 `credentials.json` → 在 **Agent 设置 → Gmail 客户自有 OAuth** 导入 → 点击“连接 Gmail”。Desktop app 不填写 Authorized JavaScript origins 或 Authorized redirect URIs；若用户已有 Web application Client，要求其新建 Desktop app，不要建议填写 localhost 或回调 URI。不得要求用户在对话中提供文件路径、JSON、Client Secret 或 Token。

## 二、首次接管流程

首次接管不发送邮件、不批准 Approval、不启用自动化。

按顺序执行：

1. 读取 Gmail 首次历史导入状态。未完成时，向用户说明范围和影响并取得明确授权，
   再启动全部正常邮件（不含 Spam/Trash）的可恢复导入。
   同步层会解码 RFC 2047 Subject 和 MIME 正文，并在 Gmail 声明错误 charset
   时使用 raw MIME 与安全的可逆乱码修复。历史损坏字段可在重同步时修复，
   AuditLog action 为 `gmail_mime_decode_repaired`。
2. 等待首次导入完成后，说明首次历史分拣会处理固定本地快照并取得新的明确授权；它按 50 个线程一批后台运行，支持暂停/恢复/取消/重试，不会创建 Draft、Approval 或发送。
3. 首次历史分拣完成后，日常同步只读取 Gmail History 增量变化，日常分拣只处理新增或变化后仍未分拣的线程。
4. 先判断邮件方向：入站、外发或混合线程。
4. 过滤广告、垃圾、Newsletter、系统通知、验证码和无关营销邮件。
5. 对剩余邮件判断是否为真人直接沟通。
6. 仅对确认是真人且属于业务客户候选的对象创建或更新 Contact；真人判断通过不等于联系人准入通过。
7. 从明确自我介绍或签名提取姓名、公司；不能猜测。
8. 在 **Agent 设置** 确认全局 Profile。姓名、公司、语言、语气和签名是
   LangGraph 强制运行配置；知识库只负责事实。Global Inbox 不需要 Campaign，
   Campaign 缺失时自动继承全局 Profile。
9. 根据完整往来更新标签、阶段和下一步动作。
10. 向用户汇报扫描数量、过滤原因、联系人变化、待处理事项和风险。

## 三、日常运营链路

### 收件箱

```text
同步邮件 → 方向判断 → 噪音过滤 → 真人判断 → 联系人准入 → Contact → 标签/阶段更新
→ 生成回复 Approval → full_auto 发送 / semi_auto 一次确认后发送 → 回复原 Gmail thread
```

纯外发线程不得生成客户回复。`human_review` 不等于 `needs_reply`；联系人准入阶段必须明确选择：Approve 由人工录入、Reject 加入非客户过滤名单、Agent Decide 仅对身份明确的真人业务客户自动建联打标。`full_auto` 也不得跳过该准入层。

### 获客

```text
Contacts → 创建 Campaign → 圈选联系人 → AI 生成首封邮件
→ 检查执行策略 → full_auto 真实发送 / semi_auto 等待一次确认后真实发送
```

### 自动跟进

```text
创建 Automation → 设置 Cron → Huey 扫描到期任务
→ 生成跟进 Approval → full_auto 发送 / semi_auto 一次确认后发送 → 回复/拒绝/退订后停止
```

## 四、真实发送规则

- Draft、queued、running、HTTP 200 都不是发送成功证明。
- 只有 Gmail 返回发送成功，才能报告 `sent`。
- 发送前必须向用户报告准确的收件人、主题、正文摘要和发送范围。
- semi_auto 未获得确认时，只能保留冻结的 Draft/计划；full_auto 在全部强制策略通过后可以发送。
- `full_auto` 由 Agent 决定 Approval 与 Human Review；`semi_auto` 由用户在冻结计划上作一次最终确认。两种模式都不得绕过 suppression、停止规则、发送窗口和幂等保护。
- 发送状态不明确时，先查询 Approval、Automation Run 和 Gmail 结果，不得重复发送。
- Gmail 同步发现同一 thread、同一收件人和同一正文已经存在外发邮件时，会将
  遗留的 pending Approval 自动对账为 `expired`，取消未发送 Draft，并记录
  `approval_reconciled_sent`。这不是一次发送操作。
- 已确认不应继续发送的 pending Approval 使用
  `POST /api/approvals/{approval_id}/invalidate` 作废。作废后检查
  `/api/approvals` 与 Dashboard Pending Approvals；不得通过批准来“清理”它。

## 五、Agent 汇报格式

每次执行后报告：

```text
执行时间：
Gmail 账号：
同步线程/邮件数：
过滤数量及原因：
真人联系人判断：
新增/更新 Contact：
标签和阶段变化：
Needs Reply：
生成 Approval/Draft：
实际发送：
停止数量及原因：
Run 状态：
下一次计划：
需要用户确认：
```

## 六、异常处理

遇到页面 Loading、API 失败、Consumer 不健康或任务长时间 queued/running：

1. 先运行健康检查。
2. 查看 `logs/backend*.log`、`logs/consumer*.log`、`logs/frontend*.log`。
3. 检查是否存在旧进程或重复 Consumer。
4. 使用 `start-stack.bat` 做统一干净重启。
5. 不直接修改数据库，不删除 OAuth Token，不重复发送邮件。

## 七、禁止事项

- 不读取、打印或转发 `.env`、API Key、Client Secret、OAuth Token。
- 不伪造联系人、邮件、指标、发送结果或送达结果。
- 不把广告、垃圾邮件和系统邮件创建为 Contact。
- 不把“真人联系人”自动等同于“销售线索”。
- 不为了回复邮件创建虚假的 Campaign；日常回复可以使用独立 Approval。
- 不在 semi_auto 未确认时发送真实邮件；full_auto 必须通过全部强制策略才可发送。
# Dual-mode execution contract

The production Agent is **full-auto first**. In `full_auto`, the Agent completes
sync, triage, CRM updates, draft generation, policy validation, send, Gmail result
verification, and reporting. In `semi_auto`, it completes all preparation, freezes
the resulting plan, reports it, and waits for one explicit confirmation before
sending the frozen plan. A confirmation does not regenerate content or recipients.

Use `POST /api/agent-runs` with `{automation_id, mode}`. Inspect the preparation at
`GET /api/agent-runs/{run_id}` and call `/confirm` or `/cancel` only for a run in
`awaiting_confirmation`. Reports must distinguish prepared, sent, blocked, failed,
and stopped items. All mandatory policy gates remain active in both modes.

## Automation scope

Select **Global Inbox** to run the top-level Agent without first creating a Campaign.
It processes triaged, Contact-admitted business threads with a followable next action.
Select **Campaign** only for a specific outreach plan. Both scopes support full-auto
and semi-auto execution independently.

Before enabling Global Inbox, select exactly one takeover scope: last X days,
all historical business mail, or future-only. This choice is mandatory and
defines the earliest inbound message eligible for the first autonomous run.

Customer queue semantics are separate from email direction:
`waiting_for_customer` means we already replied; `follow_up` is shown only when
`next_follow_up_at` exists. Delivery reports distinguish Gmail accepted
(`gmail_sent`) from Gmail sync-confirmed (`verified`).

## Semi-auto reporting and confirmation

`semi_auto` is the supervised operating mode. The Agent completes sync, triage, Contact/stage updates, intent analysis, content generation, policy checks, Draft creation and Approval preparation, then enters `awaiting_confirmation`.

The pre-send report must include the exact recipient, original Gmail thread, subject, full body, reason for reply, risk level, Draft/Approval IDs, skipped items, policy blocks, idempotency result and the 24-hour expiry. The Agent must wait at this single point and must not send or regenerate the plan.

When the user confirms, the Agent dispatches the existing frozen plan and reports Gmail's actual result separately from preparation: sent count, Gmail message IDs, blocked/stopped/failed counts, final Run status and thread verification. A Draft, pending Approval, queued Run or HTTP 200 is not a sent result. If the user does not confirm, the Agent reports `no_send` after expiry or cancellation.

## Agent Profile acceptance check

Before final customer acceptance, open **Agent 设置** and verify name, company,
role, tone, language policy, signature, forbidden claims and Campaign override.
For English-only operation select `english`; test with both Chinese and English
inbound messages and confirm that the reply body remains English while retrieved
Chinese knowledge is still used accurately.

Verify that `Best, 1`, numeric Campaign identities and placeholders are rejected.
Invalidate any bad Approval and confirm its Draft is cancelled before rebuilding.
Use Gmail disconnect/reconnect for expired OAuth tokens; the operation must
preserve contacts, knowledge, Campaigns and historical mail.

## Uncertain special-content review

Only definite system, advertising, and spam mail is automatically filtered. A job
application, forwarded message, or scripted/automation message is kept in human
review whenever the current sender's identity, business relevance, or intended
action is uncertain. It does not create a Contact, Draft, Approval, reply, or
send. For an unknown sender, use the Contact-admission decision; for an existing
Contact, the operator may confirm no action while retaining the Contact.

`full_auto` and `agent_review` never bypass this review. They may operate only
after the review is resolved and all existing server-side safety checks pass.

## Embedded TACWork acceptance

For customer-facing operation, the right-side TACWork panel is part of the Email
Automation Web experience. A first-time operator should be able to start the
Workspace, open the Web UI, and use the resident Agent without separately finding
or launching TACWork.

Acceptance checklist:

- `start-stack.bat` starts Email Automation Backend, Consumer, Frontend, TACWork
  Server, TACWork Web, and OpenCode Engine.
- `scripts/portable-health.ps1` reports Email Automation health and TACWork
  connectivity without printing secrets.
- The Dashboard right panel loads TACWork for the locked Email Automation
  Workspace.
- Browser refresh restores the last non-archived session instead of showing a
  fresh `New session`.
- The explicit plus/New button creates a new session only when the operator asks.
- The panel remains usable at reduced browser widths or provides a clear collapsed
  state; it must not hide the Email Automation status badges or block Dashboard
  operations.
- TACWork can answer read-only takeover questions using `AGENTS.md` and `docs/`
  without performing Gmail sync, Draft creation, Approval decisions, sends,
  Automation enablement, DB edits, `.env` edits, or OAuth changes unless the user
  explicitly authorizes that class of action.

Recommended final delivery test prompt for TACWork:

```text
Read AGENTS.md, docs/AGENT_OPERATIONS.md, docs/AGENT_CRON.md and
docs/AGENT_ENABLEMENT_GUIDE.md. Perform a read-only final acceptance check of the
current Email Automation Web experience and embedded TACWork panel.

Do not modify source, config, database, .env, OAuth, Gmail, Contacts, Campaigns,
Approvals, Drafts, Automation, or reports. Do not sync Gmail and do not send mail.

Verify service health, Gmail connected account, pause state, real-send flag,
Dashboard readiness, pending Approvals, frozen Runs, TACWork panel loading,
session restore after refresh, explicit New session behavior, localhost/127.0.0.1
connectivity, reduced-width layout, and browser console/runtime errors if visible.

Report execution time, exact checks performed, pass/fail results, user-visible Web
issues, business safety observations, and whether the Workspace is ready for
customer handoff.
```

## Portable handoff expectation

A customer Zip may include bundled Python/Node runtimes and offline dependency
caches, but it must not include live secrets, OAuth tokens, business databases, or
logs by default. The expected operator flow is:

```text
install -> launch Email Automation.exe -> wait for the app readiness gate
-> import customer-owned Google Desktop OAuth credentials.json in Web Setup
-> complete authorization in the system browser -> use the embedded TACWork panel
```

The embedded Agent may guide the user through model configuration and Gmail OAuth,
but it must not invent credentials, print secrets, or bypass the Web OAuth flow.
Only an `installed` Desktop OAuth JSON is accepted. Google login must open in the
system browser, never inside Electron or TACWork. `oauth_not_configured`,
`oauth_not_connected`, `oauth_connected`, and `credential_key_unavailable` are
distinct states; saving a file is not proof that Gmail is connected.

## Operational computer migration

The portable customer package is intentionally a fresh-install artifact and must
not contain live `.env` values, encryption keys, OAuth credentials, operational
databases, queue files, TACWork session data, or logs. It is not a move of an
already operating Workspace.

To move an active Workspace to a new computer, stop the old stack, use an approved
secure migration channel for the operational data directory and its matching
credential/encryption configuration, then start the new stack with
`start-stack.bat`. Do not copy active PID/status files or rely on an old TACWork
session as continuation context. A data directory and its matching encryption key
must move together; otherwise Gmail must be connected again on the new computer.

Before relying on Agent Takeover, verify Backend and Consumer health, Gmail,
pause state, TACWork panel/MCP connectivity, and `GET /api/agent-takeover`. The
schedule runs only while that computer and the unified stack are running; it does
not wake a shut-down or sleeping computer. Enable/reconfirm takeover only after
these checks. The next due interval creates a new TACWork root session.
