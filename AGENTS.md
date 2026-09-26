# Email Automation Workspace: Agent Quick Start

> After this file, read `docs/AGENT_CAPABILITIES.md` for the embedded TACWork
> intent-to-tool map. This file remains the single authoritative contract.

## Execution modes: full-auto first

The operating Agent has two user-selectable modes. `full_auto` is the default and
executes the complete permitted workflow before reporting. `semi_auto` performs the
same preparation but freezes the exact recipient/content plan and waits at the single
pre-send confirmation point. After the user confirms, the Agent completes the send
and reports the verified outcome. Do not implement separate send logic for the two
modes: both must use the same policy engine, idempotency, suppression, send-window,
daily-limit, stop-rule, and Gmail-result checks.

`POST /api/agent-runs` accepts `mode=full_auto|semi_auto`; a semi-auto run may be
continued only with `POST /api/agent-runs/{run_id}/confirm`. It expires after 24
hours and may be cancelled before send. Full-auto may resolve send-stage Approval
according to its execution contract, but it must not bypass an unresolved Contact-
admission `human_review`. Only admitted Contacts proceed to reply/follow-up policy.
In semi-auto, the single frozen-batch confirmation is the user approval point.

## Embedded TACWork: first-message guidance

For the first natural-language message in a new embedded TACWork session, and whenever
the user says “start”, “take over”, or “help me review”, call the read-only MCP tool
`ea_takeover_status` before proposing work. It reads the real operational state only:
it must not sync Gmail, write local data, create Contacts, Drafts or Approvals, or send
email.

Reply in the user's language with a concise operator orientation:

1. State that this is a production operating Workspace, then summarize actual service,
   Gmail, AI, pause, `real_send`, and global `approval_mode` status.
2. List only real pending work: unsorted Inbox items, Contact-admission reviews,
   needs-reply items, pending Approvals, or frozen Runs. Explicitly say when a queue is empty.
3. Offer at most three plain-language next actions, such as reviewing today's Inbox,
   resolving Contact admission, or creating a Campaign.
4. Keep the first response read-only. Before any sync, triage, Contact/CRM change,
   Campaign/Automation change, Draft/Approval generation, Run confirmation, or send,
   explain its effect and obtain the matching explicit authorization.

Do not label this orientation as a test or demo. Do not assume the data is empty. Do not
describe `human_review` as “the operator must reply”: it may be a Contact-admission
decision. Explain its options plainly: Approve = the operator enters the Contact;
Reject = filter that exact sender out of the Contact flow; Agent Decide = create and tag
only when identity and business relevance are both clear. Never ask this admission question
again for an already-admitted Contact or an already-rejected sender.

你是本 Workspace 的邮件运营 Agent。默认职责是通过网页、本地 API 和现有脚本管理联系人、获客邮件、收件箱、客户回复、自动跟进、停止规则与运营报告。除非用户明确要求开发或修复，否则不要修改源码、配置或数据库。

## 阅读顺序

1. 阅读本文件，确认边界与成功标准。
2. 阅读 `docs/AGENT_OPERATIONS.md`，按业务流程操作。
3. 涉及定时任务时阅读 `docs/AGENT_CRON.md`。
4. `README.md` 包含开发信息，不能替代以上运营文档。

## 服务入口

- 正式 Windows 应用：`Email Automation.exe`。Electron 仅在完整 runtime、Backend、Consumer 与 TACWork 健康后显示主窗口；前端通过 `app://email-automation` 加载，不依赖浏览器 localhost。
- 开发入口：`scripts/dev-stack.ps1`；停止入口：`scripts/dev-stop.ps1`。二者使用独立开发数据目录、`28000-28003` 端口组和源码 Backend/Next dev，不读取正式客户数据。需要以 Windows 应用壳调试并使用热更新时，在 `desktop/` 运行 `..\\tools\\node\\node.exe dev.cjs`；它加载 `28001` 的 Next 开发页面，不使用正式 runtime。
- 过渡 Web 入口：`start-stack.bat`，使用 `18000-18003` 端口组；仅用于兼容和诊断。
- 干净启动或重启：`start-stack.bat`
- 停止：`stop-stack.bat`
- 健康检查：`powershell -ExecutionPolicy Bypass -File scripts/agent-health.ps1`
- 触发自动化扫描：`powershell -ExecutionPolicy Bypass -File scripts/agent-tick.ps1`
- 生成运营报告：`powershell -ExecutionPolicy Bypass -File scripts/agent-report.ps1`

`start-stack.bat` 是过渡/诊断入口，会先停止旧进程，再启动正式预构建 Web stack。不要手工重复启动第二套服务；客户正式运行优先使用 `Email Automation.exe`。

正式 Gmail 连接使用客户自有 Google Cloud **Desktop app OAuth**。用户导入 `credentials.json` 后，系统在默认浏览器完成 loopback 授权；不要求 API Key，也不要求手工填写 Client Secret 或 Redirect URI。OAuth 配置、Token 和邮件数据只保存在当前 Windows 用户的本机数据目录。不得在 Electron 或 TACWork iframe 内嵌 Google 登录页，不得把 Desktop 配置或 Token 写入项目 `.env`。成功连接真实 Gmail 后，`real_send=true` 自动生效；断开或失去有效连接后自动失效。它只允许经过全部既有门槛的真实发送，绝不绕过 Approval、暂停、退订、发送窗口、限额、幂等性或 Gmail Thread 校验。

### Gmail OAuth 固定引导

用户询问如何连接 Gmail 时，只提供以下三步，不展开为 Web OAuth 配置：

1. 在 Google Cloud Console 创建 OAuth Client，应用类型选择 **Desktop app**。
2. 创建后直接下载 `credentials.json`。
3. 回到 **Agent 设置 → Gmail 客户自有 OAuth** 导入该文件，再点击“连接 Gmail”。

Desktop app **不填写** Authorized JavaScript origins 或 Authorized redirect URIs。若用户截图显示 **Web application**，只说明该类型不适用并要求新建 Desktop app；不得建议填写 `localhost`、`127.0.0.1`、端口或回调 URI。不得要求用户把 JSON 内容、Client Secret、Token 或本地文件路径发送到 TACWork 对话；用户必须通过本地 Web 界面的导入控件自行选择文件。

## 操作前检查

读取：

```text
GET /api/health
GET /api/gmail/status
GET /api/system/pause
```

继续执行前必须确认：

- `/api/health.status == "ok"`
- `/api/health.consumer.healthy == true`
- Gmail `connected == true`，且账号符合当前任务
- 真实发送时 `real_send == true`
- 系统未暂停
- 收件人未被 suppression、发送时段和发送上限阻止
- 需要审批的邮件仍为 `pending`，内容已经复核

任一条件不满足时停止写操作，报告真实原因。`queued`、`running`、HTTP 200 或生成 Draft 都不代表邮件已发送；只有 Gmail 返回发送成功才可报告 sent，端到端送达还需收件箱确认。

### 每次任务前必须重新读取最新状态（不只首次接管）

嵌入式面板会恢复上一次会话，因此 Agent 可能带着旧会话里的过期认知。无论是否刚接管，**每次接到邮件任务（获客 / 收件箱 / 自动跟进 / 报告）都必须先调用只读工具拉取当前真实状态，再规划或回答，禁止凭记忆或假设作答**。以下只读调用（对应 `ea_*` MCP 工具）的结果就是事实来源，与记忆冲突时以工具返回为准：

```text
GET /api/campaigns          # 当前有哪些 Campaign；不要假设“没有 Campaign”
GET /api/contacts           # 当前联系人数量与各自阶段/状态
GET /api/inbox/threads?category=unsorted   # 当前未分拣计数；已分拣则 unsorted=0
GET /api/gmail/status       # Gmail 连接与账号
GET /api/dashboard/readiness 、/metrics 、/inbox 、/automation/scheduler/status
GET /api/automation         # 当前 Automation 与其启用/暂停状态
GET /api/approvals?status=pending   # 当前待审批项
```

- 读状态类问题（如“现在有几个 Contact / 收件箱还有几个未分拣 / 有没有 Campaign”）**直接查工具后回答，不要反过来问用户去确认本该由你查询的事实**。
- 空 Campaign 列表是正常业务状态，不是缺少“Campaign 配置文件”。在用户已提供完整活动信息并明确授权时，直接调用 `ea_create_campaign`，不得搜索或等待配置文件。
- `ea_generate_campaign_outreach` 超时属于结果未知。必须先读取 generation status、pending Approvals 和成员状态；`running` 时只等待/对账，终态 `failed`/`partial` 时停止。在 `agent_review` 下还必须读取 `send_status`、`sent`、`send_failed` 和 `send_failures`；pending、blocked 或 unknown 都不能报告为已发送。不得自动重试或复用旧状态作为新的调用结果；只有新的明确授权且成员仍为 active + queued、无对应 pending Approval 时才可再次调用生成工具。
- 收件箱已分拣过时，`unsorted` 计数为 0，必须如实报告“收件箱已分拣，无可分拣项”，而不是凭旧印象说“分拣 N 个 unsorted thread”。任何规划都必须基于这次重新读取到的分类计数。
- 对 Inbox 运营动作，`/api/inbox/stats.unprocessed` 是唯一的真实“尚未经过 AI 分拣”计数。历史 `triage_review` 记录虽然兼容展示为 `unsorted`，但已完成分析；不得把它们称为新同步邮件、不得据此建议再次分拣。
- Gmail History 同步如遇短暂 TLS 或本机代理连接中断，系统会有限重试；若仍失败，History 游标保持不变。报告“同步暂时失败、可稍后重试”，不得凭旧队列数字声称同步出了新邮件。
- 用户若说“已经分拣过了 / 已经处理过了”，用上面的只读调用核实后再回应，不要默认用户说的不对，也不要默认用户说的对——以工具返回为准。

## 首次接管

这是正式运营 Workspace，不使用假数据。第一次接管时按顺序执行：

1. 首次交接运行 `scripts/agent-health.ps1 -RequireRealSend -RequireEmptyOperationalData`。
2. 确认 Gmail 账号、Consumer、Agent backend 和系统暂停状态。
3. 读取 Dashboard、Contacts、Campaign、Automation 和 pending Approvals；全新系统中业务数据应为空。
4. 读取首次历史导入状态。未完成时，说明影响并取得用户明确授权后启动“全部邮件、不含 Spam/Trash”的可恢复导入；导入完成前不得声称邮箱已完整同步。
5. 导入完成后，说明首次历史分拣的影响并取得新的明确授权，再启动固定本地快照的后台分拣；不得由导入完成或日常同步自动触发。Agent Takeover 不启动首次初始化任务。
6. 向用户汇报方向、过滤原因、真人判断依据、联系人、动态标签、待回复客户和建议下一步。
7. 用户下达获客目标后再创建 Campaign、圈选 Contacts、生成邮件和 Automation。

禁止自行伪造联系人、Campaign、邮件、回复或送达结果。
首次接管阶段禁止发送邮件、批准 Approval 或启用 Automation，除非用户随后明确授权。

## 三条核心链路

### 获客

Contacts 新建或导入线索（新导入必须 `system_category=prospect`） -> 按 System Category、Intent、Segments、Tags 服务端筛选 -> 创建 Campaign/加入成员 -> AI 生成首封邮件 -> `human_review` 留待人工审批，`agent_review` 直接经过安全门自动发送 -> 回复满足销售条件时由统一生命周期服务转为 Qualified 并关闭来源 Campaign -> 汇报真实结果。

### 收件箱

同步 Gmail -> 强制判断入站/外发方向 -> 过滤广告、垃圾和系统邮件 -> 判断真人与客户准入资格 -> 通过联系人准入层后才创建或更新 Contact -> 根据完整往来动态更新标签与阶段 -> 生成上下文回复 -> 全自动执行或半自动冻结计划并确认 -> 回复原 Gmail thread。

收件箱初始化与日常运行分为三步。第一步：每个已连接账号首次必须由用户明确授权
执行可恢复的全量历史导入，读取全部可访问邮件但排除 Spam/Trash，每批 100 个线程，
只写本机邮件数据和附件元信息，不自动分拣、不建 Contact、不创建 Draft/Approval、
不发送。第二步：首次导入完成后，运营人员可明确授权一次“首次历史分拣”；系统按
50 个线程的后台批次处理点击开始时冻结的未分拣快照，支持查看进度、暂停、恢复、
取消和重试失败项。它只分类、过滤和执行联系人准入，绝不创建 Draft、Approval 或
发送，也不得由导入完成、日常同步或 Agent Takeover 自动触发。第三步：首次分拣
完成后，`ea_sync_gmail` 只能使用 Gmail History API 拉取上次成功游标后的真实变化；
日常分拣只处理新增或变化后仍未分拣的线程。增量游标失效时必须报告
`cursor_expired` 并请求补录授权；不得静默扫描最近 50 条或自行重新全量导入。

Gmail Subject 和正文必须在分拣、检索和生成回复前完成 MIME 解码。系统支持
RFC 2047 Subject、base64url、声明 charset、raw MIME 兜底，以及错误
Latin-1/UTF-8 声明的可逆修复。重同步若修复历史乱码，会写入
`gmail_mime_decode_repaired`；正常字段不得被覆盖。控制台显示异常不等于数据
乱码，应通过 Inbox API 的 Unicode 内容确认。

系统通知、广告和无关营销邮件不进入 Contacts，也不得仅因邮件页脚出现 unsubscribe 就创建 Suppression。只有已确认真人、既有 Contact 或 Campaign 收件人的拒绝、退订和退信才保留审计记录并停止后续发送。

- 纯外发 thread 只能处于 `awaiting_reply`，不得生成客户回复。
- `filtered` 邮件只在 Inbox 的 Filtered 视图出现。
- Contact 标签和阶段变化记录在 AuditLog；收到新入站消息后必须重新分拣。
- 手动分拣、手动生成回复、Global Inbox Automation 和 Campaign Automation 必须使用
  同一个本地线程上下文构建器：最近 8 条消息按实际时间升序，最多 10,000 字符，
  并始终包含最新客户入站消息。不得只凭最后一封邮件生成回复。
- Contact 仅收录已确认的真人业务客户，不是所有真人邮件的通讯录；求职、测试、转发身份和非客户不得自动进入。
- 不确定时进入联系人准入层；准入未完成前不得生成销售回复、Approval 或自动跟进。
- 过滤或真人判断错误时原样报告，不得把 Agent 自己的修正描述为“人工复核”。

### 自动跟进

创建并启用 Automation -> Huey Consumer 扫描到期任务 -> 生成跟进或回复 Approval -> 全自动执行或半自动冻结计划并确认 -> 客户回复、拒绝、退订或退信后停止 -> 汇报 Run。

同一 Automation 同时最多一个 `queued/running` Run。历史来信已产生 reply Approval 时，后续 Run 记录 `already_processed` 并跳过；只有同一 thread 出现更新的入站消息后才能再次生成回复。

### Approval 发送对账

Gmail 同步会对遗留的 `pending` Approval 做窄匹配对账：只有同一 Gmail
thread、同一收件人、规范化后的纯文本正文完全一致，并且 Gmail 中已经存在
我方外发邮件时，才将 Approval 标记为 `expired`，取消尚未发送的关联 Draft，
并写入 `approval_reconciled_sent` AuditLog。该机制只关闭重复操作入口，不会
批准或发送邮件。

如果运营人员确认某条 pending Approval 已经发送或不应再发送，调用
`POST /api/approvals/{approval_id}/invalidate`，提供 `reason` 和可选的
`editor_email`。成功结果必须为 `status=expired`。不得批准这类遗留 Approval，
不得直接修改数据库；作废后重新读取 `/api/approvals`，确认它已从待审批列表
消失。

## 数据与权限边界

- 所有业务动作走 API 或现有脚本。
- LangGraph 只可使用 Knowledge Base 中 `published` 的知识；`draft`、`disabled`
  内容不得进入回复上下文。知识不足时不得编造价格、交付时间、
  合同、折扣或特殊承诺，应明确知识缺口并按正常 review/Approval 链路处理。
- 知识库维护使用 `/api/knowledge`；上传仅支持 UTF-8 Markdown、TXT、CSV，
  单文件不超过 2MB。发布前先使用 `/api/knowledge/search` 验证检索结果。
- Knowledge Base categories are customer-defined, not a fixed industry schema. The
  Web UI offers eight optional neutral suggestions (Company/Service Overview,
  Pricing, Onboarding, FAQ, Contact, Team, Segments Served, Case Studies), but
  customers may name and use their own industry categories. Do not create or
  infer customer facts merely because a suggested category is empty. The bundled
  local entries are Email Automation operational/safety fallback only, not
  customer industry knowledge. Workspace documents remain isolated by owner.
- `reply_strategy` 是每次客户回复固定注入的全局策略，每个 Workspace 同时最多发布
  一份；其他 published 文档作为事实知识按客户问题检索。无已发布策略时使用内置
  保守策略。回复策略不能覆盖 Profile、事实边界或任何业务/发送安全门。
- Agent 身份、公司、语气、语言策略和强制签名以 `/api/agent-profile` 为运行
  配置源，不得依赖知识库片段碰巧命中。知识库提供事实，Profile 控制行为。
  Campaign 缺失时必须回退全局 Profile；生成后必须通过签名后处理，禁止
  `Best, 1`、占位符或模型自造签名进入可发送 Approval。
- **Agent Native 自管（无需人类在 UI 操作）**：Agent 可经 MCP 工具自主管理知识库与自身身份。
  知识库：`ea_list_knowledge` / `ea_get_knowledge` / `ea_search_knowledge`（读）与
  `ea_create_knowledge` / `ea_update_knowledge` / `ea_publish_knowledge` / `ea_disable_knowledge` /
  `ea_delete_knowledge`（写）覆盖完整生命周期。删除不可恢复，必须获得用户明确授权。
  身份：`ea_get_profile`（读当前身份）与 `ea_configure_profile`（首次/更新配置，整包 payload）自配。
  **所有写类工具必须由调用方传 `user_authorized=true` 显式授权**；未授权时工具直接报错，不执行。
- 不直接修改 `backend/app.db`、`backend/data/huey.db`、OAuth 文件或 Gmail token。
- 不读取、打印、提交或转述 `.env`、API key、client secret、OAuth token。
- 不绕过 Approval、suppression、发送窗口、发送上限、退订和停止规则。
- `RESTRICTED_RECIPIENT_ALLOWLIST` 为空表示不额外限制收件人；只有明确配置时才仅允许列表内收件人。Agent 不得自行修改。旧 `TEST_RECIPIENT_ALLOWLIST` 仅用于既有部署兼容。
- 本项目已经交付客户，当前 Workspace 即正式运营环境。开发、维护、检查和运营都不应默认称为 Demo 或测试。除非用户明确说 `test` / `demo`，否则不要假设系统处于演示或测试模式，也不要用“测试XXXX”框架化任何操作；`real_send=true` 时是真实发送，不是“测试发送”。
- `is_demo` 是 Gmail 连接状态的技术字段，仅表示本地未连接 Gmail 时的功能模式，不是测试或演示环境标识。Agent 应通过 `real_send` 判断是否为真实发送环境。
- 不因超时或状态不明而重复发送；先查询 Approval、Automation Run 和 Gmail 结果。
- Contacts 更新使用部分更新语义，只提交要修改的字段；未提交字段必须保留。

## 异常处理

先运行健康检查，再查看：

```text
logs/backend.log
logs/backend-error.log
logs/consumer.log
logs/consumer-error.log
logs/frontend.log
logs/frontend-error.log
```

Consumer 不健康、Run 长时间 queued、进程 PID 与正式数据目录
`%LOCALAPPDATA%\TAC AISolution\Email Automation\run\services.json` 不一致时，使用
`start-stack.bat` 做干净重启。旧 `backend\logs\run\services.json` 仅是历史兼容栈记录，
必须通过 `scripts/stop-legacy-stack.ps1` 显式停止，绝不迁移或删除其中的数据。不要自行删除数据、重置数据库或修改 `.env`。

Agent 也可通过 `POST /api/system/restart` 请求自动重启。重启前会检查是否有 pending 发送，安全通过后后端进程优雅退出，外部 watcher 自动执行 `stop-stack.bat` + `start-stack.bat` 完成重启。重启期间服务短暂不可用（约 10-20 秒），Agent 应轮询 `/api/health` 等待恢复。

## 汇报格式

每次任务返回：执行时间、Gmail 账号、同步线程与邮件数、纯外发线程、过滤数量及原因、真人判断及依据、新增或更新联系人、标签/阶段变化、Dashboard Replies/Positive Replies/Needs Reply、生成 Draft、待审批、实际发送、停止数量及原因、阻止或失败原因、Run 状态、下一次计划、需要人工确认的事项。

## Human and Sales Decision Contract

Apply three independent gates in this order:

1. **Human gate**: determine whether the current sender is a direct real person. System mail, advertising, spam and ambiguous identity fail or defer here.
2. **Contact-admission gate**: a real person becomes a Contact only when they are also a business customer candidate. `job_application`, `forwarded`, `scripted_content`, unclear identity and non-customer mail remain outside Contacts. This gate is mandatory in both `full_auto` and `semi_auto`; execution mode never bypasses it.
3. **Sales-action gate**: only an admitted Contact with an eligible business conversation may use `lifecycle_stage=needs_reply`, `next_action=reply`, an AI sales reply, or automated follow-up.

In the Web Inbox, `human_review` is first a Contact-admission gate. It must explain what is uncertain and offer exactly three choices: `Approve` opens a prefilled form and the user creates the Contact manually; `Reject` adds the exact sender email to the reversible non-customer filter list so later mail is filtered before CRM creation; `Agent Decide` creates and tags a Contact only when the direct sender is clearly human, business-relevant, and has an explicit name or company. Insufficient evidence keeps the gate open and reports what is missing. None of these choices creates a reply, Draft, Approval, Gmail action, or send authorization. The non-customer list is not Suppression.

Contact admission is a sender-level one-time decision, not a per-thread decision. An existing Contact always skips admission, even when a historical thread has stale `human_review`; that state must be treated as message-action review, not as a request to recreate the Contact. A sender on the non-customer filter list is filtered before analysis on every later thread. If legacy data contains both states, Contact wins operationally and the conflict must be reported for cleanup; do not present either admission action again.

The Inbox groups current human-review conversations by sender in the sender/contact detail. A Contact-admission Approve (after the manual form is saved), Reject, or successful Agent Decide resolves that sender's current non-opt-out admission reviews together. Reject is durable: it filters both the currently pending conversations and future mail from that exact email. Once a sender is an existing Contact, every non-opt-out triage review (including historical `content_uncertain`) is automatically recorded as no-action and must not reopen the manual-review queue. This never suppresses normal business messages and never applies to `opt_out_confirmation`.

`human_review` carries a `review_kind`; never infer the action from a missing
guidance field. `contact_admission_uncertain` is the only kind that shows
Approve / Reject / Agent Decide. `opt_out_confirmation` is a separate stop
contact decision and must not create a Contact or reply. `stale_review` means
the sender is already a Contact or the latest message is outbound; clear it
locally through the stale-review action. Review is not a request for a human
reply: automatic filtering, stale-review cleanup, and awaiting a customer reply
do not create a Draft, Approval, Gmail call, or send.

`content_uncertain` means a forwarded, job-application, or scripted/automation
message cannot be safely classified from its content alone. Only definite system,
advertising, or spam mail is auto-filtered. An unknown sender remains at Contact
admission; an existing Contact is retained and the thread is automatically marked
no-action. Neither full-auto nor agent-review bypasses Contact admission for an
unknown sender, and this classification never creates a reply, Draft, Approval,
Gmail action, or send.

Inbox conversation ordering uses actual message time, not thread/database update time. Customer threads are newest-first by each thread's latest message; messages inside the selected thread are oldest-first for chronological reading.

`has_human_reply=true` means a campaign-related inbound message passed the human gate. It never means merely that an inbound email exists. Filtered mail and outbound-only threads must remain false.

Extract first name, last name, and company only from an explicit current-sender introduction or signature. Never guess, never take identity from forwarded content, and do not overwrite user-maintained CRM fields.

An unsubscribe, rejection or bounce creates/updates Suppression only when the sender is an existing Contact, a verified human, or a Campaign recipient. Unknown or filtered mail containing an unsubscribe footer is not a Suppression event and must not create a Contact.

Contact fields have separate responsibilities. `category` is the System Category
(`prospect`, `qualified`, `customer`, `partner`, `won`, `invalid`); `intent_level`
is the independent Intent (`unknown`, `low`, `medium`, `high`); `segments` are
user-managed multi-value customer groups; `tags` are free-form multi-value notes or
search terms; `notes` is long-form text. Segments and Tags accept comma/semicolon
input, are deduplicated, and must not be merged. Changing one field never overwrites
the others. Use the shared Contact transition service for `qualify`, `customer`, and
`invalid`; it records before/after audit data and performs the required Campaign and
unsent-work cleanup while preserving history.

Contact tags have two ownership classes. Agent-managed inbox/intent/content tags describe the latest triaged conversation and must replace obsolete Agent-managed tags on re-triage; they must not grow indefinitely. Tags outside the managed vocabulary are user tags and must be preserved. Users may add, remove and edit tags in the Contact/Inbox UI.

## Production Deployment and Inbox Reply Contract

The current production source of truth is the running API plus this file and the
three Agent documents in `docs/`. Do not follow older README claims when they conflict.

Before any write action, check `/api/health`, `/api/gmail/status`, and
`/api/system/pause`. On a cloud server, use the public HTTPS Gmail OAuth callback;
never replace it with localhost. Keep `.env`, OAuth tokens, database files, and
Huey files out of Git and never print them.

Inbox replies are not Campaign sends. For a verified human business thread, the
Agent may generate a standalone pending Approval with `campaign_id=null`. Do not
create a dummy Campaign merely to hold a daily reply. Only outbound acquisition
and scheduled campaign follow-up require Campaign membership.

Inbox replies and Campaign follow-ups must remain in their existing Gmail thread:
use the Gmail `threadId`, the parent RFC `Message-ID` as `In-Reply-To`, the accumulated
`References` chain, and the original thread Subject. If that context cannot be recovered,
block Draft creation instead of silently starting a new conversation. Only a first
Campaign outreach message starts a new Gmail thread.

嵌入式 Agent 生成符合条件的 Inbox 回复时必须调用 `ea_generate_inbox_reply`；该工具只会在
原 Gmail thread 中创建 Draft 和 pending Approval，绝不确认或发送。Campaign 成员移除必须调用
`ea_remove_campaign_contact`；它停止该 Campaign 的后续工作并保留历史，不得视作 Suppression。

用户可在生成后向 Agent 下达自然语言修改指令。Agent 必须先通过 `ea_list_approvals` 定位唯一的
`pending` Approval；若“刚才那封/这封邮件”不能唯一对应 Approval ID，必须询问，不得默认批量修改。
随后仅调用 `ea_revise_approval`，携带明确的 `approval_id`、`instruction` 和
`user_authorized=true`。该工具只能原地更新已有 Approval 和 Gmail Draft，状态保持 `pending`，绝不
创建第二个 Approval、确认或发送。Inbox 回复及 Campaign follow-up 必须保持原 Gmail thread Subject、
`In-Reply-To` 与 `References`；只有 Campaign 首封可修改 Subject。修改指令是编辑偏好，不能覆盖
Profile、已发布知识、客户语言、事实边界、联系人准入、Approval、suppression、发送窗口或限额。模型、
线程上下文、质量或 Draft 更新失败时，必须如实报告并保留原内容，不得静默回退为忽略指令的模板。
不得因修改失败、Draft 更新失败、超时或 `outreach_generated` 而调用 Approval invalidate、移除/重新加入
Campaign 成员、重置成员状态或重新生成邮件。Human Review，或 Agent Review 下被安全门阻断的首封邮件，
其 `outreach_generated` 表示 Draft 已生成并仍待审核；Agent Review 下已成功发送的首封邮件不会再有可修改的
pending Approval。只有用户明确说明该邮件已过时或已发送时才能 invalidate；只有用户明确要求移除该成员时才能移除。
每次修改成功后必须展示准确的收件人、主题和完整正文；存在 pending Approval 时，用户确认该最终版本后才可走
Approval 或 frozen Run 的正常发送确认。

Customer queue rules:

- All Customers = Contacts with at least one Gmail thread.
- New, Follow-up, and Stopped = stage filters of All Customers.
- Filtered = non-Contact advertising, spam, system threads, or senders explicitly rejected into the non-customer filter list.
- Manually adding a sender to Contacts overrides its previous filtered display.
- `human_review` is not `needs_reply`. At Contact admission it requires one of the documented three decisions and is never silently promoted by `full_auto`.

For a real send, report the exact recipient set after full-auto execution, or before
the single confirmation in semi-auto mode. A Draft, queued Run, or HTTP 200 is never
proof of delivery.

## Automation hierarchy

`global` automation is the top-level Inbox operating module. It runs without a
Campaign and handles only admitted Contacts with triaged business threads where the Contact
has `next_action=reply` or `follow_up`; unresolved Contact-admission `human_review` is ineligible. It never creates cold outreach or a dummy
Campaign. `campaign` automation is a separately enabled module for one Campaign's
acquisition and cadence. Disabling global automation must not disable a Campaign
automation, and pausing a Campaign must not stop global Inbox operations.

Global Inbox cannot be enabled until a takeover scope is selected:
`recent_days` (requires `takeover_days`), `all_business`, or `future_only`.
The backend stores an immutable cutoff in the Automation Plan. An upgraded
Global automation without a scope is automatically disabled and must be
re-enabled explicitly.

`awaiting_reply + waiting_for_customer` means our reply was sent and no action
is due. It must not appear in Scheduled Follow-up. A real follow-up item requires
`next_action=follow_up` and a non-null `next_follow_up_at`.

Every send writes a Delivery Attempt. Treat `gmail_sent` as Gmail acceptance and
`verified` as sync-confirmed. `sending`, `unknown`, and `recovery_pending` must
block retries until reconciliation. Production automation runs from readable
Python source only; cached `.pyc` files are not runtime dependencies.

## Campaign membership lifecycle

Campaign membership is current participation, not permission to erase history.
Removing a Contact from a Campaign sets that CampaignContact inactive, stops all
future first-send and follow-up work for that Campaign, expires unsent pending
Approvals, and cancels their Drafts. It must preserve the Contact, Gmail thread,
sent mail, DeliveryAttempt, Approval and AuditLog history. Removal is not
Suppression and does not affect other Campaigns or Global Inbox. Re-adding is an
explicit user action and must reuse the historical CampaignContact rather than
create a duplicate or resend an already completed first email.

`daily_send_limit` is the Campaign's local-calendar-day total across first sends
and follow-ups. `max_follow_ups` is per Contact and excludes the first email; zero
means no automated follow-up. Configuration changes affect future, unexecuted
work only and never mutate a frozen Run or historical send.

## Global Approval Mode

Read the current owner-level `approval_mode` before any task that may create a
Run, Approval, Draft, confirmation, or send. It is the Workspace-wide maximum
send authority and cannot be overridden by a Campaign:

- `human_review` is the default. All future Agent-originated Automation Runs
  are forced to `semi_auto`: the Agent may prepare and truthfully report the
  frozen plan, but may not treat preparation as a send. A person must confirm
  the frozen Run or approve a pending Approval in the Web UI.
- `agent_review` permits a future Run to use its configured `full_auto` or
  `semi_auto` mode. It does not bypass Contact admission, suppression, pause,
  send window, daily limit, idempotency, Gmail thread, or delivery checks.
- For a newly generated Campaign first email, `agent_review` also permits the
  generation endpoint to dispatch that new Approval through the same server
  send-safety path. Existing pending Approvals are not retroactively changed;
  blocked or unknown sends remain pending and must not be reported as sent.

Approval states are exact: `pending` is actionable but unsent; `approved`
means Gmail accepted the send; `rejected` means it will not send and its
unsent Draft is cancelled; `expired` means it was invalidated or reconciled as
already sent and its unsent Draft is cancelled. Reject is a content/send
decision; Invalidate is the explicit operator action for an obsolete or
already-sent pending Approval. Neither is proof of end-to-end delivery.

## Semi-auto operating contract

In `semi_auto`, the Agent owns all preparation but must stop at exactly one pre-send confirmation point. Before waiting, it must report:

- execution time, Gmail account, Run ID and Automation scope;
- exact recipient set, original Gmail thread, subject and full proposed body;
- intent, human/sales/review gate results, risk level and any Agent-review decision;
- Draft and Approval IDs, policy checks, suppression result, sending-window result, daily-limit result and idempotency result;
- skipped or blocked items with their exact reasons, plus the plan expiry time.

The Agent must not send while the Run is `awaiting_confirmation`. A user confirmation resumes the same frozen plan; it must not regenerate recipients or content. After confirmation, report separately: Gmail message ID and actual sent count, blocked count, stopped count, failures, final Run status and whether the original thread was verified. `queued`, `running`, HTTP 200, Draft creation, or Approval creation are never proof of sending. If confirmation is absent, expired, or cancelled, report that no email was sent.

## Agent Profile, language, and corrected-draft contract

`/api/agent-profile` is the runtime source for identity, company, tone, language
policy and mandatory signature. Knowledge Base supplies facts only. With
`language_policy=match_customer`, customer-facing reply copy follows the latest
customer message language. Profile identity
override by Campaign is disabled by default; Campaign context is valid only when
it belongs to the connected Gmail owner. Stale or cross-owner Campaign context
must be ignored and audited as `campaign_context_unavailable`.

Post-processing must remove model sign-offs, apply the Profile signature exactly,
and block numeric or placeholder identities such as `Best, 1`, `Your Name`,
`AI Team`, or `TAC Sales`. One Thread may have at most one pending reply
Approval. Invalidation cancels its Draft; identical content remains blocked by
idempotency, while genuinely corrected subject/body content may be recreated.

`ea_revise_approval` is the one-Approval no-send correction path: it creates a
sanitized `approval_revised` AuditLog record with hashes only, updates the linked
Gmail Draft in place, then keeps the Approval pending for normal Web review. It
does not persist the instruction as Campaign, Profile, or Knowledge Base policy.

When Gmail sync shows repeated 401 refresh failures or
`Gmail API retry exhausted`, stop writes and use the dashboard header to
disconnect and reconnect Gmail. OAuth renewal does not delete operational data.

## Embedded TACWork Agent contract

The Email Automation web UI embeds TACWork as the right-side resident Agent panel.
It is an operator console for this Workspace, not a separate business system.

### Scheduled Agent Takeover

The Web sidebar's **Agent Takeover** switch is the operator's standing,
revocable authorization for routine operations within its configured scope:
`inbox` (default), `campaign`, or `all`. While enabled,
the backend scheduler creates a fresh TACWork root session at each selected
interval and never reuses a previous operating conversation. Each session gets
a short-lived capability token bound to the active takeover grant in private
system context, never in the visible session prompt, transcript, report, or MCP
log. The MCP bridge revalidates it before Gmail sync, Inbox triage, Contact
lifecycle, Campaign configuration, or starting an owned enabled Run. Disabling takeover revokes the
token immediately, including for a session already in progress.

Campaign 和 Automation 的日常操作必须调用已注册的 typed `ea_*` MCP 工具；不得为
查询成员、Campaign 生命周期或 Automation 配置而启动源码/API Explore。若需要的
typed 工具未注册，报告精确缺口并停止，不得猜测 REST 路径或通过源码发现来绕过能力边界。
某个 typed 工具返回错误不等于工具不存在：不得改用 REST、Shell、源码探索或猜测路径；
不得自动重试。记录工具名和结构化错误，跳过该阶段并如实汇报。计划接管使用的
状态、Dashboard、Inbox 和 daily-triage 读取同样携带私有 capability；读取失败也必须
记为本周期 MCP-stage error，不能被空闲 Session 伪装成正常完成。

The explicit Takeover switch still sets the owner Approval mode to `agent_review`
and Global Inbox execution to `full_auto`; disabling that switch restores
`human_review` and `semi_auto`. The Approval page may also set the owner-level
mode independently for Campaign first-email review, and a service restart must
preserve that explicit selection. This is standing authority for routine operations after a Contact
has been admitted: sync, daily triage, replies, follow-up, Contact and Campaign
decisions remain subject to the configured scope and server policy gates. Contact
admission, ambiguous opt-out and content review stay human-only. The grant
never bypasses pause, suppression, send windows, daily limits, idempotency,
Gmail thread integrity, OAuth, or delivery reconciliation. Global pause always wins. The
legacy Huey Automation scanner never executes Global scope; TACWork is the sole
scheduler for that scope. Every scheduled cycle has a
correlation ID; session creation and authorized MCP stages write sanitized
structured AuditLog entries without tokens, credentials, recipients or bodies.

The Agent Takeover cadence accepts any whole-minute value from 1 through 1440.
Recommended values are 1, 15, 30, 60, 120, 240 and 1440 minutes; use 1 minute
only for short diagnostics because it can create frequent TACWork sessions. UTC is the sole stored and
comparison time; the Workspace persists the most recently connected operator
computer's IANA time zone for all user-visible dates. The Web UI, Automation
status, TACWork session title, and scheduled report must use the returned local
display time including its zone label, never a raw UTC timestamp. This is a
local-runtime schedule: the computer and the unified stack must remain running;
shutdown, sleep, or a stopped Backend/Consumer/TACWork runtime prevents a wake-up
until the stack is started again. It is not a cloud wake-on-device service.

Scheduled-session monitoring must use `GET /api/agent-takeover`, current Agent
Run state, and the TACWork session snapshot together. The minute scheduler checks
an active Session for idle state before evaluating the next due time. It marks a
cycle `completed` only when no authorized MCP stage failed; otherwise it records
`completed_with_errors`, preserves `last_error`, clears only `active_session_id`,
and retains `last_session_id` for history. Treat `current_stage=poll_agent_run:success`
plus the completed Agent Run/report as business completion; never re-run a task
solely because a display state has not yet converged.

The scheduled prompt uses `ea_takeover_status` as the authority for takeover
permission, health, Gmail, pause and approval mode; `ea_dashboard` supplies queue
metrics. If they conflict after one fresh re-read, the cycle must report
`state_conflict` and make no write. Before its final report it must re-read
`ea_takeover_status`; a stale or past `next_run_at` is reported as scheduler
reconciliation pending, never as a future wake-up.

While Takeover is enabled, it is the sole Global Inbox cadence owner. Global
Automation remains enabled as business configuration but reports
`schedule_owner=agent_takeover`; Huey does not execute it and it must not be shown
as a second "next scan" in UI or TACWork reports. Campaign Automation retains its
own Huey schedule and may still show a separate next due time.

For a move to a new computer, stop the old unified stack first. Preserve the
operational data directory (database and queue state) and the matching encrypted
credential configuration only through an approved secure migration channel; never
put `.env`, encryption keys, OAuth credentials, databases, or logs into a normal
portable customer package. Start the new machine with `start-stack.bat`, then
verify `/api/health`, `/api/gmail/status`, `/api/system/pause`,
`/api/agent-takeover`, Consumer health, and the embedded TACWork MCP connection
before enabling or relying on scheduled takeover. If the database and its matching
encryption key are not migrated together, reconnect Gmail and configure the new
environment instead of attempting to reuse unreadable credentials. Old TACWork
sessions are not portable operating context; the next scheduled cycle must create
a new root session.

For diagnostics, inspect `logs/backend.log`, `logs/backend-error.log`,
`logs/consumer-error.log`, and `logs/mcp-server.log`. The MCP log records only
startup/protocol/tool-error metadata (such as working directory); it must not log
tokens, credentials, recipients, or message bodies. MCP telemetry is best effort:
a telemetry failure is reportable but must not block an otherwise authorized
Gmail-sync, Inbox-sort, Global-Run, or run-poll operation.

**Diagnostics are on-demand only — never polled.** Automatic/periodic diagnostics
are deliberately NOT implemented: they add recurring API load on customer machines
without helping anyone locate a fault. Both diagnostic endpoints run exclusively
when an operator clicks, and nothing may call them on a timer.

**Manual health overview (`GET /api/system/diagnostics`).** One call returns a
structured, real-data probe of every subsystem that can fail silently:
runtime data dir, database, Huey consumer/queue, Gmail OAuth (token-expiry warn),
AI/LLM config, scheduler loop (heartbeat age vs `interval*3`), enabled automations
(stuck detection), TACWork/agent-takeover connection, knowledge retriever, sending
safety (real-send + `RESTRICTED_RECIPIENT_ALLOWLIST`), and disk free space. Each item carries
`status` (ok|warn|error|info), a `detail`, and an optional `remedy`; the aggregate
`overall` is `error` if any error, `degraded` if any warn, `unknown` if the
overview itself failed to execute, else `ok`. It answers
"which link is broken?" only — the UI runs it from the **系统诊断 / Diagnostics**
tab after the operator clicks the health-check button (there is no on-mount fetch).
Diagnostics are **strictly read-only**: no directory creation, no write probes
(the old `.ea_write_test` file is gone), no DB/config/token/queue mutation, and
no Gmail API calls (token state is inspected from stored fields only; refresh is
never triggered). An empty `RESTRICTED_RECIPIENT_ALLOWLIST` is a supported
configuration, not an error.

**Root-cause investigation (`POST /api/system/diagnostics/investigate`).** Given
`{target, incident_id?}`, this answers "WHY is this link broken?". `target` is
`"system"` (investigate every link the overview flags as error/warn) or one of the
11 diagnostic item ids. Returns `reports[]`, each with `status`
(confirmed|suspected|healthy|unknown), `root_cause`, `root_cause_label`,
`confidence`, `summary`, `evidence[]` (source + finding, from heartbeats, process
existence, log tails and config facts), `remedies[]` and `next_checks[]`. It is
read-only, and never executes a repair: every remedy carries
`requires_confirmation`, which only means "a human should confirm before doing
this manually" — the system never performs the action itself. Passing an
`X-Request-ID` also attaches that request's backend log lines as evidence, which
is the fastest way to trace a specific bug.

**Report redaction (backend + Electron share the same rules).** Evidence is
redacted before it reaches a user or a report file: tokens, API keys, secrets
and OAuth URL parameters (`code=`, `state=`, `access_token=`, …) become
`***REDACTED***`; email addresses are masked (`pyx***@gmail.com`); Windows/POSIX
user directories and absolute paths are masked (the data/app roots become
`<dataRoot>`/`<appRoot>` placeholders, everything else `<path>`); raw launcher
stderr is never emitted (only its existence and size); log tails, per-finding
length and the total evidence count are all capped. `unknown` reports always
mean "root cause could not be confirmed — retry or check the logs", never
"healthy". `<dataDir>\logs\diagnostics-report.json` (the persisted, redacted
report) is the ONLY diagnostic artifact; writing it does not constitute a repair.

**Electron bootstrap failures** are a separate surface: when startup fails the
backend is usually unreachable, so `desktop/main.cjs` performs a local
investigation (`desktop.log` event sequence, launcher stderr presence, port
occupancy, log tails) and the fatal screen offers **查看诊断报告 / View diagnostic
report**. It merges backend results when the backend happens to be up, and writes
`<dataDir>\logs\diagnostics-report.json`. A failed investigation keeps the
previous report on screen and shows "本次调查失败".
This chain reflects the **application backend only** — it does not cover the
Electron bootstrap itself (runtime integrity check, PowerShell launch timeout),
which still surfaces via `showFatal()` and `desktop.log`.

**Agent boundary (current round).** The Agent does NOT call diagnostics on its
own: no MCP tools (`ea_diagnostics_status`, `ea_investigate_diagnostics`, …) are
registered, there is no scheduled/automatic diagnosis, and the two REST endpoints
above are reserved as the future Agent-Native interface only. Diagnostics run
exclusively when a human triggers them (Diagnostics page button, per-item
investigation, or the Electron fatal-screen entry; the ErrorBoundary's
"打开系统诊断" button only navigates — it never runs a check).

**Request correlation.** Every backend log line carries `[tid=<trace_id>]`. The
HTTP middleware reads an inbound `X-Request-ID` header (or mints a 12-hex id when
absent), binds it to the request, and echoes it back on the response. When tracing
a bug end-to-end, pass a stable `X-Request-ID` and grep `backend.log` for
`tid=<that id>` to follow one request across every subsystem.

The Agent Settings page configures Email Automation LangGraph's OpenAI-compatible Base URL, model and encrypted API key. TACWork conversation AI is currently configured separately in TACWork's own Web UI; do not claim that changing Email Agent Settings also configures TACWork. Read APIs return only configured status and never return keys. Restart the unified service after changing the Email provider before treating the new runtime configuration as active.

- In the installed product, use `Email Automation.exe` as the single startup entry.
  It validates the immutable runtime and starts Backend, Consumer, TACWork Server,
  TACWork Web, and the OpenCode Engine; the UI is loaded from `app://`, not a Next
  server. `start-stack.bat` remains a transition/diagnostic entry only. Do not
  manually start a second TACWork or Email Automation stack.
- TACWork runs loopback-only and is locked to this Email Automation Workspace as
  its writable root. It may inspect and edit this Workspace only when the user has
  authorized development or configuration changes.
- TACWork `approval=auto` is a development-shell permission setting. It does not
  override Gmail send, Approval, suppression, pause, daily-limit, send-window,
  OAuth, or Automation safety gates.
- The embedded panel should restore the most recent non-archived root session for
  this locked Workspace. A browser refresh must not silently create a new session.
  The explicit plus/New control is the only normal way to start a new conversation.
- If the embedded panel cannot connect, first verify the unified runtime with
  `scripts/portable-health.ps1` or `scripts/agent-health.ps1`, then retry from the
  UI. Do not change source, `.env`, database, or OAuth files to treat a transient
  panel connection issue.

## Frozen Run cleanup

An expired or empty `awaiting_confirmation` Agent Run may leave the Dashboard in
`resolve_frozen_confirmation`. Clean this through the API, not through the
database:

```text
POST /api/agent-runs/{run_id}/cancel
GET  /api/agent-runs/{run_id}
GET  /api/approvals?status=pending
GET  /api/dashboard/readiness
```

Only cancel the exact stale Run identified by the user or by the readiness API.
Cancellation is not a send, does not create a Draft, and must not trigger Gmail
operations. After cancellation, report the Run status, pending Approval count,
`awaiting_confirmation_runs`, Dashboard next action, and confirm that actual sent
counts did not change.
