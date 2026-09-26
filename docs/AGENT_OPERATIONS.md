# Agent Operations Manual

## Gmail deployment recovery (2026-09-06)

- Production Gmail operations fail with `gmail_credentials_unavailable` if
  credentials cannot be used. Do not report an empty mailbox or successful
  import in this case. Reconnect through the local OAuth UI. In-memory Gmail
  is reserved for explicitly enabled offline tests/development.
- Quota, authorization, server and parse failures preserve the durable sync
  cursor. Retry the failed operation; never manually advance the cursor to
  the latest profile value. A deleted thread (HTTP 404) may be skipped.
- First-import status includes `phase=scanning|history_replay|completed`.
  The final scan page commits `scan_completed` with its data. Resume the same
  failed Run to replay History without re-scanning completed pages. Upgraded
  legacy Runs without this marker conservatively scan again; do not infer
  completeness from an empty page token.
- Backend health includes `instance.install_root` and `instance.data_root`.
  Startup recovery checks both plus process ownership. A healthy service in
  another installation or data directory must be reported as a conflict, not
  adopted. Older services without identity require an explicit controlled stop.


> `AGENTS.md` is authoritative for modes, safety gates and send reporting. This
> document contains procedures only; MCP mappings are in `AGENT_CAPABILITIES.md`.

> Transition Web runtime: `start-stack.bat` also starts the bundled TACWork Server,
> TACWork Web and OpenCode Engine. TACWork runs loopback-only with
> `approval=auto`; its writable root is locked to this Email Automation
> Workspace. Verify the complete stack with `scripts/portable-health.ps1`.
> This does not bypass Email Automation send, approval, pause, suppression or
> other business safety gates.

## Global Approval Mode

Before generating a Campaign first email, or starting or confirming an Automation Run, read `/api/agent-profile` and
report `approval_mode`. `human_review` is the Workspace default and forces all
future Agent Runs to `semi_auto`; the Agent may prepare a frozen plan but a
person must confirm it or approve a pending item in the Web UI. `agent_review`
permits the configured Run mode, including `full_auto`, only after all existing
server safety checks pass. Campaign configuration cannot override this global
limit. For a newly generated Campaign first email, `agent_review` also permits
the generation endpoint to dispatch that new Approval through the same
server-side send-safety path. Existing pending Approvals are not retroactively
sent when the mode changes; a blocked or unknown dispatch remains pending.

Approval `rejected` means no send and the unsent Draft is cancelled. `expired`
means the operator invalidated it or sync reconciled it as already sent; its
unsent Draft is also cancelled. Do not confuse either state with delivery.

## Revising generated pending copy

When an operator asks the embedded Agent to revise an already generated reply or
Campaign first email, the Agent first lists pending Approvals and identifies one
exact Approval ID. It then calls `ea_revise_approval` with the operator's explicit
instruction and `user_authorized=true`. This is a no-send operation: it updates
the existing pending Approval and its linked Gmail Draft in place, then reports
the revised subject/body for human review. It must not create a replacement
Approval, approve it, or send it. An Inbox reply or Campaign follow-up retains its
original Gmail Thread headers and Subject; only first outreach may change Subject.
If the target is ambiguous, the AI is unavailable, the safety checks fail, or the
Draft update fails, leave the original content unchanged and report the exact
blocker. The instruction is one-time editorial guidance, never stored as a
Campaign/Profile/Knowledge Base setting.

`outreach_generated` means a Campaign first-email Draft was already prepared; it
is not a retry error and must not be reset by removing the member. A failed copy
revision must never trigger Approval invalidation, Campaign-member removal, or a
replacement Draft. Only a direct operator instruction that the item is obsolete
or already sent may invalidate it; only a direct operator instruction may remove
a Campaign member. After each successful revision, show the exact revised
recipient, subject and body. For Human Review or a blocked Agent Review
dispatch, the final approval or semi-auto confirmation is the release point for
sending; an Agent Review dispatch that already succeeded has no pending Approval
to revise or release again.

本项目已经交付客户，当前 Workspace 是正式运营环境。开发、维护、检查和业务操作均按正式环境标准执行；除非用户明确指定 `test` / `demo`，不得默认使用 Demo、测试或非正式运营框架描述任务。

本手册用于运营 Agent 操作 Email Automation。前端用于人工操作与审计，API 是 Agent 的稳定执行入口。

## 1. 启动与健康检查

使用 `start-stack.bat` 启动或重启。脚本仅在归属验证通过后清理本应用的旧 Backend、Consumer 和 Frontend，记录本次 PID 到正式数据目录 `%LOCALAPPDATA%\TAC AISolution\Email Automation\run\services.json`，并等待所有服务健康。旧 `backend\logs\run\services.json` 是历史兼容栈记录，需显式使用 `scripts/stop-legacy-stack.ps1` 停止，不迁移或删除其数据。

检查：

```text
GET /api/health
GET /api/gmail/status
GET /api/system/pause
GET /api/agent/health
```

最低通过标准：

- Backend `status=ok`
- `consumer.healthy=true`，`consumer.state=running`
- Electron 正式入口显示 `Frontend (Electron app://): static runtime ready` 即为前端健康；不要求监听本机前端端口
- 仅使用 `start-stack.bat` 的兼容 Web 栈要求 `http://127.0.0.1:18001` 返回 HTTP 200
- Gmail `connected=true`
- 系统 `paused=false`
- 真实发送任务中 `real_send=true`

如果 Consumer 心跳缺失或超过 150 秒，健康接口返回 `consumer.healthy=false`。不要继续触发 Automation；执行一次干净重启并复查日志。

正式环境默认不配置收件人 allowlist，即允许任意收件人继续进入 suppression、Approval、幂等、发送窗口与发送上限检查。allowlist 一旦被配置就成为额外限制；运营 Agent 不修改该配置。

### 空白系统首次接管

1. 运行 `scripts/agent-health.ps1 -RequireRealSend -RequireEmptyOperationalData`。
2. 确认 Contacts、Campaign、Automation 和 pending Approval 均为空。
3. 检查首次历史导入状态；经用户明确授权完成全部邮件（不含 Spam/Trash）的可恢复导入。
4. 导入完成后，经新的明确授权启动首次历史分拣。它冻结当时的未分拣线程快照，以 50 个线程一批后台执行；不与导入混同，也不自动启动。
5. 分拣严格执行：方向门控 -> 噪音过滤 -> 真人判断 -> 联系人准入判断 -> Contact 创建/更新 -> 内容贴签。
6. 先汇报过滤依据、真人判断、Contact、动态标签、指标和下一步，再按用户目标创建 Campaign。
7. 未获得运营目标前，不自行群发或启用 Automation。

## Scheduled Global Inbox operation

The sidebar Agent Takeover is the sole cadence owner for Global Inbox operation.
It creates a fresh TACWork root session for each interval; the legacy Huey scanner
never executes Global scope. Campaign Automation remains separately Huey-scheduled.
All user-visible schedule and report times use the current Workspace display
timezone shown by TACWork on the local operator computer; UTC is retained only
for storage and comparison.

## 2. Contacts CRM

Contacts 是 Inbox、Campaign 和 Automation 的统一客户来源。

```text
GET  /api/contacts
POST /api/contacts
POST /api/contacts/import
PUT  /api/contacts/{contact_id}
POST /api/contacts/{contact_id}/transition
```

文件导入和显式的线索录入至少需要合法邮箱，以及 `first_name` 或 `last_name` 之一。新线索必须使用 `system_category=prospect`；导入后默认 `intent_level=unknown`、`lifecycle_stage=new_customer`、`next_action=review`、`status=new`。旧版没有分类列的 CSV 只能走兼容模式，默认 Prospect 并返回兼容警告。Inbox 自动接管不得仅凭“是真人”创建 Contact；还必须确认其属于业务客户候选。Agent 自动创建时必须从当前发件人的正文或签名明确取得姓名或公司，不得只用邮箱占位，不得猜测身份。

`PUT` 是部分更新，只提交要修改的字段。未提交字段保持不变；显式传入 `null` 才会清除允许为空的字段。更新后重新读取联系人，核对身份、分类与标签。

`manual_lock=true` 表示人工 CRM 判断优先，AI 不覆盖普通分类和销售字段；退订、拒绝、投诉、退信等安全停止信号仍必须执行。

Contact 是统一客户池中的一条记录，可表示从 Prospect 到 Qualified、Customer 或 Invalid 的生命周期；它不是通过 Lead 表复制出来的第二份联系人。文件导入的 Prospect 是用户明确提供的线索，不等同于 Inbox 未知发件人的自动准入。求职、测试、转发身份和明确非客户的 Inbox 邮件仍不得绕过 Contact-admission gate 进入 Contacts。

字段职责必须保持独立：System Category (`category`) 是 `prospect`、`qualified`、`customer`、`partner`、`won`、`invalid` 等运营分类；Intent (`intent_level`) 是 `unknown`、`low`、`medium`、`high` 的意向强度；Segments 是用户管理的可多选客户分群，例如 `IT`、`Finance`；Tags 是可自由输入的多值备注/检索词，例如 `demo_request`、`met-event`、`priority-client`。Segments 和 Tags 都支持逗号或分号输入并去重，但不合并；Notes 只是长文本备注，不参与自动资格判断。

自动创建和动态状态更新分别写入 AuditLog：

```text
contact_created_from_human_email
contact_state_updated_from_conversation
```

第二种记录包含 `before`、`after`、`thread_id` 和本次 intent。Agent 不得把自己的自动修正称为人工复核。

### Contact lifecycle transitions

分类转换必须统一调用 `POST /api/contacts/{contact_id}/transition` 或
`ea_transition_contact`，不能由 Inbox、Campaign 和 UI 各自实现副作用：

| Action | Result |
| --- | --- |
| `qualify` | Sales-related reply (`interested`, `asking_question`, `objection` or the existing sales rule) changes the Contact to Qualified, maps Intent to high/medium, closes only the source Campaign membership as `converted`, and cancels that Campaign's unexecuted Draft/Approval/follow-up work. |
| `customer` | Explicit user or authorized Agent transition to Customer; it may close one explicitly selected source Campaign, but never creates another Campaign automatically. |
| `invalid` | Stops the Contact, closes all active Campaign memberships and cancels future work. Confirmed opt-out remains subject to the existing human review gate. |

All transitions preserve Contact, Gmail thread, sent mail, Approval, DeliveryAttempt and AuditLog history. A Contact in multiple Campaigns cannot be qualified against an inferred source; the Agent must identify the source Campaign first. A Contact with no active Campaign cannot be qualified by an Inbox-only reply. A Contact with no qualifying reply remains Prospect and stays eligible for the current Campaign cadence.

## 3. Campaign 获客

空 Campaign 列表表示当前尚未创建活动，是正常状态；它不是“Campaign 配置文件”缺失。用户给出完整名称、活动说明、目标客户、语言/语气等信息并明确授权后，直接按 typed MCP 顺序创建、加成员、核验成员、启动和生成，不要在源码或磁盘中查找配置文件。

Campaign 生成调用如遇超时，结果必须视为未知。先读取 generation status、
`send_status`、`sent`、`send_failed`、`send_failures`、pending Approvals、
成员状态和 Gmail 结果：`running` 时等待并对账；`failed`/`partial` 时停止并
报告真实原因。`agent_review` 下生成和发送是两个需要分别对账的阶段，
`generated` 不等于 `sent`；`completed` 也必须结合 Gmail 结果确认实际发送。
不得自动重试或以旧状态代替新的工具调用结果。只有取得新的明确用户授权，
并重新确认成员为 active + queued、没有对应 pending Approval 后，才可再调用
一次生成工具；生成后立即重新读取结果。

标准流程：

1. `GET /api/contacts` 选择目标联系人。
2. `POST /api/campaigns` 创建 Campaign。
3. `POST /api/campaigns/{id}/contacts` 添加成员。
4. `POST /api/campaigns/{id}/start` 激活。
5. `POST /api/campaigns/{id}/generate` 生成首封邮件。`human_review` 保留
   pending Draft/Approval；`agent_review` 对本次新生成的 Approval 继续走
   同一服务端发送安全链路。
6. 读取 generation status、发送统计和 `GET /api/approvals?status=pending`；
   阻断或结果未知的邮件仍需人工复核，不能报告为已发送。
7. 对仍为 pending 且明确需要人工放行的 Approval，用户授权后调用
   `POST /api/approvals/{id}/decision`。
8. 查询 Approval 和 Gmail 结果，必要时由收件邮箱确认送达。

嵌入式 TACWork Agent 执行上述日常链路时，必须依次使用已注册的 typed MCP 工具：
`ea_find_contacts`、`ea_update_contact`、`ea_transition_contact`、
`ea_list_contacts`、`ea_create_campaign`、`ea_add_campaign_contacts`、
`ea_start_campaign`、`ea_generate_campaign_outreach`，以及需要时的
`ea_generate_automation_plan`、`ea_create_automation`、`ea_enable_automation` 和
`ea_schedule_automation`。加入成员前后用 `ea_list_campaign_members` 核验结果。
不得为了查找普通业务接口而派生 Explore/源码检索任务；若工具缺失或服务拒绝，
如实报告该操作不可用或失败，并停在现有安全边界。

典型的线索筛选链路是：先用 `ea_import_contacts` 预览并验证所有新行的
`system_category=prospect`，再用 `ea_find_contacts(category=prospect,
segments_any=[IT])` 获取确定的联系人 ID，得到用户授权后加入 Campaign。收到当前
Campaign Thread 的明确销售回复后，调用 `ea_transition_contact(qualify,
campaign_id=...)`；该操作只关闭来源 Campaign，不自动创建下一轮 Campaign。

Approval 至少检查收件人、联系人身份、Campaign 匹配、主题、正文、重复发送、事实准确性、suppression、发送窗口和每日上限。

Gmail Draft 创建失败时不得创建 `draft_id=null` 的 Approval。HTTP 409 必须保留真实策略原因，禁止自动重试或把错误改写成 draft not found。

### Campaign member removal and limits

`daily_send_limit` is the Campaign's total first-send plus follow-up allowance per
local calendar day. `max_follow_ups` is per Contact after the first email; `0`
disables automated follow-up. Editing these values affects future unexecuted work
only and must not rewrite a frozen Run or historical send.

Use `ea_remove_campaign_contact` in the embedded Agent (or `DELETE /api/campaigns/{id}/contacts/{contact_id}` in the Web UI) to remove a Contact from
future participation without deleting history. The operation expires unsent
pending Campaign Approvals, cancels their Drafts and follow-up tasks, and retains
the Contact, sent mail, Gmail thread, DeliveryAttempt and AuditLog. It is not an
unsubscribe or Suppression and does not affect other Campaigns or Global Inbox.
Re-add only after explicit user authorization; the server reuses the historical
membership and must not repeat an already completed first send.

## 4. Smart Inbox

### Knowledge Base

运营人员通过前端 Knowledge Base 或 `/api/knowledge` 维护 LangGraph 可用知识。
支持粘贴 Markdown/纯文本，以及上传 UTF-8 `.md`、`.markdown`、`.txt`、`.csv`
文件（最大 2MB）。新内容可以先保存为 `draft`；只有 `published` 内容参与
邮件回复检索，`disabled` 内容立即退出检索。删除是永久操作且不可恢复。

`reply_strategy` 是每次客户回复固定读取的全局回复策略，每个 Workspace 同时
最多发布一份。其他分类作为事实知识按客户问题检索。回复策略不能覆盖 Profile、
联系人准入、人工判断、Suppression、Approval 或发送安全规则。

知识类别由客户自行定义，不是固定行业枚举。Knowledge Base 页面提供 Company /
Service Overview、Pricing、Onboarding、FAQ、Contact、Team、Segments Served、Case
Studies 八类中性建议，但客户可以直接输入、重命名或新增行业专属类别。参考模板为
`audit/kb-content-template.md`；它不包含任何客户或行业预置事实。内置知识仅用于
Email Automation 的运行与安全兜底，不能当作客户业务事实。所有客户文档按 Workspace
owner 隔离，只有该 owner 的 `published` 文档可参与检索。

发布前调用 `POST /api/knowledge/search`，使用真实客户问题检查返回的来源、
版本、片段与相关度。价格、合同、折扣、交付日期和特殊承诺没有明确已发布
依据时，Agent 不得自行补全。知识库变更会写入 AuditLog。

### Global Agent Profile

前端 **Agent 设置** 或 `GET/PUT /api/agent-profile` 管理结构化运行配置。首次
运营及任何真实发送前，确认 Agent 名称、公司、角色、语气、语言策略和强制
签名。知识库中的人设文字仅为参考，不能替代 Profile。

配置优先级为：系统硬性安全规则 → 全局 Agent Profile → 允许的 Campaign
覆盖 → 已发布知识 → 邮件线程。无 Campaign 或线程引用的 Campaign 已失效时，
使用全局 Profile 并检查 `campaign_context_unavailable` 审计记录。Approval
正文必须以 Profile 的签名原样结束；出现 `Best, 1`、`Your Name`、`AI Team`
等异常签名时禁止发送并重新生成。

日常增量同步（仅在首次导入完成后）：

```text
POST /api/gmail/sync
```

首次历史导入：

```text
POST /api/gmail/imports
GET  /api/gmail/imports/current
POST /api/gmail/imports/{id}/pause|resume|cancel
```

首次导入分页读取 Gmail `in:anywhere -in:spam -in:trash`，每批 100 个线程，
无总量上限并保存断点。它只同步本机邮件数据，不触发 AI 分拣或任何发送链路。
完成后须由用户明确启动一次首次历史分拣；该任务冻结未分拣快照、每批处理 50 个
线程、显示进度并支持暂停/恢复/取消/重试失败项。它仅执行 Inbox 分类、过滤和
联系人准入，绝不创建 Draft、Approval 或发送。首次分拣完成后 `/api/gmail/sync`
只消费 Gmail History 变化；游标失效时停止并请求恢复授权。

日常增量分拣：先执行 History API 同步并显示新增/变化后未分拣会话数，再创建一个
固定快照的后台 Run。Run 没有总量上限，50 仅是安全工作批大小；运行期间再次同步到
的会话保留给下一次 Run。它与首次分拣同样支持进度、暂停、恢复、取消和失败重试。
Agent Takeover 启用时可在周期内自行同步、创建和监控此日常 Run；首次全量导入和
首次历史分拣仍不自动启动。

分拣与查询：

```text
GET  /api/inbox/stats
GET  /api/inbox/initial-triage/current
GET  /api/inbox/daily-triage/current
GET  /api/inbox/triage/recent
POST /api/inbox/initial-triage
POST /api/inbox/initial-triage/{id}/pause|resume|cancel
POST /api/inbox/initial-triage/{id}/retry-failed
POST /api/inbox/daily-triage
POST /api/inbox/daily-triage/{id}/pause|resume|cancel
POST /api/inbox/daily-triage/{id}/retry-failed
POST /api/inbox/sort
GET  /api/inbox/customers
GET  /api/inbox/threads/{id}
POST /api/inbox/threads/{id}/analyze
POST /api/inbox/threads/{id}/human-review
POST /api/inbox/threads/{id}/contact
GET  /api/inbox/non-customer-filters
DELETE /api/inbox/non-customer-filters/{filter_id}
POST /api/inbox/threads/{id}/generate-reply
```

处理顺序：

1. 方向门控：没有入站消息的纯外发 thread 标记等待回复，禁止生成回复。
2. 噪音过滤：系统通知、广告、Newsletter、验证码和垃圾邮件进入 Filtered，不创建 Contact。
3. 真人判断：先确认当前发件人是真实个人；这一步通过不等于创建 Contact。
4. 联系人准入：只让真人业务客户候选进入 Contacts。`Approve` 打开人工表单，保存后才完成；`Reject` 将精确邮箱加入可撤销的非客户过滤名单；`Agent Decide` 仅在姓名或公司明确、当前发件人直发且业务相关时自动创建并打标。证据不足继续保留审核并说明缺失项。
   准入是发件人级的一次性判断：详情顶部按发件人汇总当前审核会话。Approve 保存人工表单、Reject 或成功的 Agent Decide 会一并关闭当前同类准入审核；Reject 对该邮箱后续所有 thread 生效，不重复询问。历史 thread 遗留的 `human_review` 不得让已存在的 Contact 再次出现“是否创建联系人”。已有 Contact 的 `content_uncertain` 自动记录为“无需动作”，不再显示人工审核；正常业务邮件仍按既有回复/跟进规则处理，退订确认仍必须人工决定。
5. 内容贴签：仅在 Contact 准入后，根据当前消息与历史往来更新主题、意向、阶段和下一步。
6. 退订、拒绝、投诉、退信：对已确认真人或既有客户立即停止。
7. 询价、Demo 请求、异议、明确问题：仅对已准入 Contact 生成上下文回复并等待审批。

分拣结果语义：

```text
awaiting_reply   纯外发，等待对方回复
filtered         广告、垃圾或系统邮件
unsorted         尚未处理或真人判断不确定
needs_reply      已确认真人且需要回复
valid_customer   已确认真人并出现有效互动
has_interest     已确认真人并表达异议或兴趣
rejected         已确认真人/既有客户的拒绝或退订
```

Filtered 邮件只出现在 Inbox 的 Filtered 视图；默认 Needs Action、New、Follow-up 和 All 不显示 Filtered。纯外发 thread 不得出现在 Needs Reply。

同一联系人可有多个 Gmail thread，应以联系人为工作单元查看全部会话和当前阶段。
会话列表以每个 thread 最新一封实际邮件的时间倒序排列；打开 thread 后，邮件按实际时间正序显示。不得使用分拣或状态更新产生的 `updated_at` 代替邮件时间。

每封新入站消息都会使旧线程判断失效并重新进入分拣。Agent 更新 Contact 标签或阶段时，系统写入 AuditLog 的 before/after 记录。人工锁定字段保持优先，但退订等强制停止信号仍执行。

Dashboard 指标口径：

- `Replies`：已通过真人门控并关联 Contact 的入站邮件数。
- `Positive Replies`：已确认真人且 intent 为 interested/asking_question 的线程数。
- `Needs Reply`：当前 `lifecycle_stage=needs_reply` 且 `next_action=reply` 的 Contact 数。
- `unprocessed` 才是实际尚未经过 AI 分拣的会话数。历史数据中 `triage_review` 的展示分类可能仍为 `unsorted`，但它已经分析完成，不能被表述为“新同步邮件”或再次要求分拣。
- Gmail History 同步出现短暂 TLS/代理连接中断时，系统会作有限重试；仍失败时不推进本地 History 游标。向运营人员报告“同步暂时失败、稍后重试”，不得把旧的 `unsorted` 统计误报为新邮件。
- `Pending Approvals`：真实待审核 Approval 数，不等同于 Needs Reply。

同步后必须核对这些指标与 Inbox/Contacts 是否一致。验证码、Newsletter 和系统通知不得增加前三项。

## 5. 回复质量

生成后的回复必须：

- 回复原 thread，Subject 保持 `Re:` 关系。
- 分拣或生成前确认 Subject 与正文已完成 MIME 解码。若界面/API 仍出现 `����`
  或 `æ...` 一类乱码，停止生成与发送，执行 Gmail 重同步并检查
  `gmail_mime_decode_repaired` 审计记录；不要让 LangGraph 基于乱码生成回复。
- 使用最新来信、历史会话、联系人和 Campaign 上下文。
- 回答客户提出的每个明确问题。
- 对价格、Demo、语言、时间和功能逐项回应。
- 不虚构价格、能力、案例、日期、折扣或承诺。
- 缺少事实时说明需要确认，并给出明确下一步。
- 使用客户语言，姓名、公司和签名正确。
- 不重复历史已发送内容。

不合格回复应拒绝或编辑，不能为了完成任务而批准。

## 6. Automation

```text
POST /api/automation
GET  /api/automation
GET  /api/automation/{id}
POST /api/automation/{id}/enable
POST /api/automation/{id}/pause
POST /api/automation/{id}/run-now
```

`run-now` 返回 queued Run。轮询详情直到 `success`、`partial` 或 `failed`。`already_inflight=true` 时继续监控返回的原 Run，不要重复触发。

每个 Run 检查：`status`、`approvals_created`、`drafts_created`、`follow_ups_resolved`、`replies_stopped`、`summary`、`timeline` 和 `error`。

历史回复幂等规则：

- 最新入站消息之后已存在 reply Approval，表示该消息已处理。
- Automation timeline 写入 `action=already_processed`，不再生成第二条 Approval。
- 同一 Gmail thread 收到更新入站消息后，旧 Approval 不阻止新回复。
- Approval 被批准、拒绝或仍待审批都算该条入站消息已处理，防止重复草稿。

### 遗留 Approval 对账与作废

同步后若 Pending Approvals 中仍出现实际已经发送的回复，先核对原 Gmail thread，
不要点击批准。同步服务只在以下条件全部成立时自动关闭遗留记录：

1. Approval 与外发邮件属于同一 Gmail thread；
2. 收件人完全一致；
3. 规范化后的纯文本正文完全一致；
4. 外发方向与当前 Gmail 账号一致。

匹配成功后 Approval 变为 `expired`，未发送的关联 Draft 变为 `cancelled`，
AuditLog 写入 `approval_reconciled_sent`。不同正文、不同收件人或不同 thread
不得自动对账。

需要人工作废时调用：

```http
POST /api/approvals/{approval_id}/invalidate
Content-Type: application/json

{
  "reason": "matching outbound email already exists; do not send",
  "editor_email": "agent:approval_reconciliation"
}
```

接口仅接受 `pending` Approval；成功返回 `status=expired`，不会调用 Gmail Send。
完成后重新读取 `/api/approvals`，核对待审批数量及 AuditLog。禁止直接修改
SQLite，也禁止批准一条已发送邮件对应的 Approval。

## 7. 状态与异常

Run 生命周期：

```text
queued -> running -> success | partial | failed
```

- 长时间 queued：检查 `/api/health.consumer` 和 Consumer 日志。
- 长时间 running：检查 Gmail timeout 和 stale-run 回收。
- `draft_blocked`：策略阻止，不是重试信号。
- `already_processed`：历史消息已经处理，不是错误。
- `gmail_timeout`：网络请求超时，等待 Run 终态后再决定是否重试。
- `worker_timeout`：旧 running Run 已被回收为 failed。

日志：

```text
logs/backend.log
logs/backend-error.log
logs/consumer.log
logs/consumer-error.log
logs/frontend.log
logs/frontend-error.log
```

不要通过删除记录、编辑数据库或修改 `.env` 修复运营问题。

## 7.5 人工按需诊断（Manual on-demand diagnostics）

诊断**只由用户主动触发**，当前 Agent 不自动调用诊断：

- 前端「系统诊断」页：手动【开始体检】→ 异常项【诊断此项】；
- Electron 启动失败页：【查看诊断报告】（本地取证 + 后端可达时合并结果）；
- 没有任何定时诊断、页面挂载自动请求或 MCP 诊断工具
  （`ea_diagnostics_status` / `ea_investigate_diagnostics` 未注册，REST 接口
  仅为未来 Agent Native 接入预留）。

诊断**只读，不自动修复**：

- `GET /api/system/diagnostics` 回答"哪个环节坏了"；`POST /api/system/diagnostics/investigate`
  回答"为什么坏"（11 个目标或 `system`）；
- 不创建目录、不写探针文件、不改数据库/配置/Token/队列；不刷新 OAuth 令牌、
  不调用 Gmail；`RESTRICTED_RECIPIENT_ALLOWLIST` 为空属允许配置，不是错误；
- `remedies[].requires_confirmation=true` 仅表示"该建议需人工确认后由人执行"，
  系统不会执行任何修复动作；
- `unknown` 状态表示"无法确认根因，需要重试或查看日志"，绝不表示正常；
- 证据统一脱敏（令牌/密钥/OAuth URL 参数 → `***REDACTED***`，邮箱/用户目录/
  绝对路径掩码，不输出邮件正文、收件人列表和原始 launcher stderr）；
- 唯一允许的诊断产物是落盘的脱敏报告
  `%LOCALAPPDATA%\TAC AISolution\Email Automation\logs\diagnostics-report.json`；
  落盘不代表执行了修复。

排查某次请求级 Bug 时，带稳定 `X-Request-ID` 调用接口，再在诊断报告中查看
同 `tid` 的后端日志行即可串联全链路。

## 8. 结果报告

报告必须区分：

- 生成 Draft
- 创建 Approval
- Gmail 发送成功
- 收件箱确认送达

同时报告执行时间、账号、Campaign/Automation、同步线程、联系人变化、客户回复、停止原因、阻止与失败、Run 终态、下一次执行和人工待办。

首次接管报告还必须包含：纯外发数量、各类 Filtered 数量、真人判断依据、Contact 创建触发线程、标签变更 AuditLog、乱码检查、错误 Suppression/空 Approval/重复 Contact 检查。

## Human and Sales-Action Gates

After every Gmail sync, separate identity from sales work:

1. Direction: outbound-only threads are `awaiting_reply`; never draft a customer reply.
2. Noise: system, advertising, and spam are `filtered`; they do not create Contacts and must keep `has_human_reply=false`.
3. Human: a direct real-person message can create or enrich a Contact even when it is not a prospect.
4. Sales action: only an eligible business request, interest, question, or objection may become `needs_reply` with `next_action=reply`.
5. Contact admission review: unknown sender identity or business relevance remains outside Contacts and Needs Reply until admission is decided. `Approve` means the operator completes the Contact form; `Reject` stores an exact-email non-customer filter decision; `Agent Decide` may create and tag only a clearly identified direct business sender. Forwarded, job-application, and scripted/automation content are not automatically filtered merely because of a tag: if their customer relevance is uncertain, keep them in human review. This decision never drafts or sends email and is separate from Suppression.

Web Inbox 人工复核必须是一次性决策：页面说明原因与 Agent 建议，用户选择“采纳建议 / 拒绝并忽略 / 交给 Agent 决定”。该 UI 决策仅关闭 review gate，不创建回复、Draft、Approval 或 Gmail 操作；正常邮件不应因泛化的“人工判断”阻塞。

标签规则：Agent 管理 `inbox`、真人、意图与内容标签，并在重新分拣时以最新会话结果替换旧 Agent 标签，避免同一联系人不断累积历史判断。人工在 Contacts 或 Inbox 中添加的自定义标签必须保留，且可随时编辑或删除。

`has_human_reply` is only a verified human campaign reply, never a synonym for incoming mail. Extract first name, last name, and company only when the current sender explicitly supplies them in a message or signature. Do not infer identity from forwarded content and do not overwrite manual CRM data.

## 9. Production Agent Quick Start

Use this order on every takeover or cloud restart:

1. Read `AGENTS.md`, this document, and `docs/AGENT_CRON.md`.
2. Run `scripts/agent-health.ps1`; require healthy Backend and Consumer, connected
   Gmail, unpaused system, and the intended real-send configuration.
3. Check first-import state. Start the resumable all-mail import only with explicit
   user authorization; it excludes Spam/Trash. After completion, daily sync is
   Gmail History incremental only. A cursor-expired result requires an explicit
   recovery decision and never a silent full rescan.
4. Run Inbox sorting and report filtered mail, verified humans, Contacts, stages,
   labels, Needs Reply, and Pending Approvals.
5. Never manufacture a Contact, message, delivery result, or approval.

### Contact-driven Inbox

The All Customers view is a CRM view, not a raw Gmail view. It contains only a
Contact with at least one stored Gmail thread, including a Contact added manually.
New Customer, Follow-up, and Stopped are stage filters of that same set. Filtered
advertising/system/spam without a Contact is shown only in Filtered. If a user
manually adds a filtered sender to Contacts, the Contact is shown in All Customers
and no longer in the Filtered customer list.

### Standalone Inbox replies

An inbound business conversation does not need a Campaign. After the human and
sales gates pass, call `ea_generate_inbox_reply` in the embedded Agent (or
`POST /api/inbox/threads/{id}/generate-reply` in the Web UI). The result is
a pending Inbox Reply Approval whose `campaign_id` may be null. It must retain the
Gmail `thread_id`, Contact, recipient, subject, and body. Review it in Approvals;
in semi-auto mode, explicit user authorization permits the frozen send plan; in
full-auto mode the Agent sends after policy checks. Campaigns remain for outbound
prospecting, Campaign members, and scheduled follow-up.

### Production reporting

Write reports to `reports/` and include: timestamp, Gmail account, sync counts,
filtered reasons, Contact changes, stage/tag changes, standalone Inbox Approvals,
Campaign/Automation runs, actual sends, stopped items, failures, and next action.
Dashboard `total_contacts` must match the Contacts view. `Positive Replies` and
`Needs Reply` count only Contacts with `lifecycle_stage=needs_reply` and
`next_action=reply`.
# Full-auto and semi-auto operations

For normal autonomous operations, launch an Agent Run in `full_auto`; the Agent
executes the end-to-end workflow and publishes a report after Gmail results return.
For a supervised run, launch `semi_auto`: it prepares a frozen plan and enters
`awaiting_confirmation`. Report the exact recipient set, subjects, bodies, skipped
items, and policy blocks. Only then may the user call confirm; cancellation must not
send the pending batch. Never treat queued, running, HTTP 200, or a draft as a send.

In full_auto, Human Review and pending Approval are Agent-owned decisions and are logged before dispatch; in semi_auto, the user confirmation is the final release. Both modes must stop on global pause, suppression, unsubscribe, bounce, daily limit,
sending window, idempotency conflict, or a new verified human reply.

## Global and Campaign automation

Create `scope=global` for the master Inbox automation. It requires no Campaign and
handles only already-triaged, Contact-admitted business conversations eligible for
`reply`, `follow_up`, or `human_review`; it never turns unknown mail into cold outreach. Create
`scope=campaign` for one Campaign's acquisition and cadence. These modules are
independently enabled: a Campaign may run when global automation is disabled.

### Global takeover boundary

Enabling Global Inbox requires one of `recent_days`, `all_business`, or
`future_only`. `recent_days` also requires 1–3650 days. The API rejects enable
requests without this choice. Never modify the stored cutoff during a run.

### Delivery and recovery states

Each real send has a Delivery Attempt: `prepared -> sending -> gmail_sent ->
verified`. A transport exception before a conclusive Gmail result becomes
`unknown`; it is not safe to retry. A timed-out worker with an uncertain
Delivery Attempt becomes `recovery_pending`. Query Gmail and reconcile before
any new send. Only `verified` proves the outbound exists in a synchronized Gmail
thread.

## Semi-auto operator report

For every semi-auto Run, use two reports:

1. **Pre-send report:** Run ID, mode, Gmail account, scope, exact recipient/thread, subject, complete body, intent and gate decisions, risk, Draft/Approval IDs, policy checks, skipped/blocked items and confirmation expiry. End with `awaiting_confirmation`; do not send.
2. **Post-confirmation report:** confirmed-by, confirmation time, unchanged frozen-plan ID, Gmail message ID(s), actual sent count, blocked/stopped/failed counts, final Run status and original-thread verification. Distinguish `no_send`, `blocked`, `failed` and `sent`.

Confirmation continues the same prepared Run. It must not re-run planning or silently change content, recipients or thread. If confirmation is rejected, cancelled or expires, report zero actual sends.

## Agent Profile and reply output enforcement

Use the frontend **Agent 设置** page or `GET/PUT /api/agent-profile`. Production
defaults should keep `allow_campaign_override=false`, with Sendy /
TAC AISolution and the approved signature. Knowledge documents may be Chinese or
English, but `language_policy=english` forces the final customer reply to English.

Configuration order is: hard safety rules → global Agent Profile → explicitly
allowed same-owner Campaign context → published reply strategy → relevant
published fact knowledge → thread context.
Generated copy is not sendable until post-processing removes model sign-offs,
applies the requested output language, appends the exact Profile signature, and
rejects numeric/placeholding identities.

Only one pending reply Approval is allowed per Thread. Invalidate a bad Approval
through the API; its Gmail Draft is cancelled. A corrected reply may be rebuilt
only when subject/body content actually changes. Never alter the database to
bypass idempotency.

If sync repeatedly returns 401 or `Gmail API retry exhausted`, use the header
**断开连接 / 连接 Gmail** flow, then re-check status and complete a successful
incremental sync before resuming preparation or sending.

## Embedded TACWork Web Panel

Email Automation now treats TACWork as the resident right-side Agent console. The
operator should not have to switch between two separate products for normal
takeover, monitoring, and development assistance.

Operational rules:

- Start and stop the whole stack with `start-stack.bat` / `stop-stack.bat` only.
  The startup script owns Backend, Consumer, Frontend, TACWork Server, TACWork Web,
  and the OpenCode Engine.
- TACWork is locked to the Email Automation Workspace. When it is asked to inspect
  operations, prefer read-only API checks and existing scripts. Source/config/DB
  changes still require explicit user authorization.
- The Web panel should restore the latest non-archived root session for this
  Workspace after refresh. It must not create a new session on every page load.
- The plus/New control starts a new session intentionally. Opening the session
  drawer and choosing an older session is the manual recovery path.
- If the panel shows connection retry/loading, verify direct service health before
  changing code:

```text
GET http://127.0.0.1:18000/api/health
GET http://127.0.0.1:18002/status
GET http://127.0.0.1:18003/
正式 Windows 版直接启动 `Email Automation.exe` 并等待 `app://email-automation` 主窗口；过渡 Web 诊断才访问 `http://127.0.0.1:18001/`。源码开发只使用 `scripts/dev-stack.ps1` / `scripts/dev-stop.ps1`、独立数据目录和 `28000-28003` 端口，不得读取正式客户数据。
```

For browser origin issues, both `127.0.0.1` and `localhost` origins are expected
to be allowed by the TACWork runtime. A CORS or iframe connection issue is a Web
integration issue, not evidence that Email Automation business services failed.

## Stale Frozen Run Cleanup

Dashboard readiness may surface `resolve_frozen_confirmation` when a prepared
semi-auto Run is still `awaiting_confirmation`. If the Run is expired, empty, or
superseded, clean it through the existing Agent Run API:

```text
POST /api/agent-runs/{run_id}/cancel
GET  /api/agent-runs/{run_id}
GET  /api/approvals?status=pending
GET  /api/dashboard/readiness
```

Do not confirm, resend, regenerate, or edit SQLite to clear this state. Report the
cleanup as state maintenance only. The expected safe result is:

- target Run status becomes `cancelled`;
- pending Approvals remain unchanged or zero;
- `awaiting_confirmation_runs` is removed from readiness;
- Dashboard next action returns to the real business queue, such as inbox sync,
  needs-reply review, or human-review handling;
- Gmail sent counts and Delivery Attempts do not change.

If the frozen Run references pending Approvals that are still valid, stop and ask
the user whether to cancel or continue the prepared plan.
