# Email Automation Agent Capabilities

本项目已交付客户；当前 Workspace 是正式运营环境。以下能力说明用于正式运营和受授权维护，不应默认包装为 Demo 或测试任务。

This is the action map for the embedded TACWork Agent. `AGENTS.md` remains the
authoritative safety and operating contract. The local MCP tools only call the
existing loopback API; they do not implement or bypass business logic.

## Embedded panel conversation start

On the first user message in a TACWork session, call `ea_takeover_status` before
asking the user to choose a task. Give a short, plain-language production status:
service/Gmail/AI/pause/send state, real queues, and up to three next actions. This
is read-only. Do not run sync, triage, CRM changes, Campaign/Automation changes,
Draft/Approval generation, Run confirmation, or sending until the user explicitly
authorizes that exact class of action. Treat Contact admission as a one-time
sender decision: existing Contacts and rejected senders do not go back through
Approve / Reject / Agent Decide.

| User intent | MCP tool | REST API | Boundary |
|---|---|---|---|
| Check services | `ea_health` | `GET /api/health` | Read-only |
| Check Gmail account | `ea_gmail_status` | `GET /api/gmail/status` | Read-only; interpret the explicit OAuth state; no tokens |
| Check global pause | `ea_system_pause` | `GET /api/system/pause` | Read-only |
| Review Contacts | `ea_list_contacts` | `GET /api/contacts` | Read-only |
| Review Campaigns | `ea_list_campaigns` | `GET /api/campaigns` | Read-only |
| Review Approval mode | `ea_get_profile` | `GET /api/agent-profile?create_if_missing=false` | Read `approval_mode` before any send-capable task |
| Set Approval mode | `ea_set_approval_mode` | `PUT /api/agent-profile/approval-mode` | High-impact write; requires `user_authorized=true` |
| Review Approvals | `ea_list_approvals` | `GET /api/approvals` | Read-only |
| Revise one pending Approval | `ea_revise_approval` | `POST /api/approvals/{id}/revise` | Explicitly authorized, no-send edit of the existing Approval and Gmail Draft; never creates a second Approval |
| Review Automations | `ea_list_automations` | `GET /api/automation` | Read-only |
| Review operating state | `ea_dashboard` | readiness, metrics, Inbox stats, scheduler | Read-only |
| Review Inbox queue | `ea_list_inbox` | `GET /api/inbox/threads` | `contact_admission_uncertain` is Contact admission; `content_uncertain` is no-action automatically for an existing Contact. Use Inbox `unprocessed` for actual untriaged work; legacy `triage_review` records are already analyzed even if their display category is `unsorted`. |
| Generate eligible Inbox reply | `ea_generate_inbox_reply` | `POST /api/inbox/threads/{id}/generate-reply` | Creates a thread-bound Gmail Draft and pending Approval; never confirms or sends mail |
| Review Agent Run | `ea_get_agent_run` | `GET /api/agent-runs/{id}` | Read-only |
| Generate live report | `ea_agent_report` | current read endpoints | Read-only aggregation |
| Check first import | `ea_gmail_import_status` | `GET /api/gmail/imports/current` | Read-only; reports required/running/completed and progress |
| Start first import | `ea_start_gmail_import` | `POST /api/gmail/imports` | Explicit user authorization; all normal mail, no AI sort or send |
| Control first import | `ea_control_gmail_import` | `POST /api/gmail/imports/{id}/{pause|resume|cancel}` | Explicit user authorization; no send |
| Check first history triage | `ea_initial_triage_status` | `GET /api/inbox/initial-triage/current` | Read-only progress and classification counters |
| Start first history triage | `ea_start_initial_triage` | `POST /api/inbox/initial-triage` | Explicit authorization after first import; fixed local snapshot, no Draft/Approval/send |
| Control first history triage | `ea_control_initial_triage` | `POST /api/inbox/initial-triage/{id}/{pause|resume|cancel|retry-failed}` | Explicit authorization; no send |
| Check daily triage | `ea_daily_triage_status` | `GET /api/inbox/daily-triage/current` | Read current/latest frozen incremental snapshot and progress |
| Start daily triage | `ea_start_daily_triage` | `POST /api/inbox/daily-triage` | Processes all currently untriaged new/changed threads; 50 is worker batch size only |
| Control daily triage | `ea_control_daily_triage` | `POST /api/inbox/daily-triage/{id}/{pause|resume|cancel|retry-failed}` | Resumable, no Draft/Approval/send |
| Read latest triage result | `ea_recent_triage_result` | `GET /api/inbox/triage/recent` | Read-only summary, timestamps and snapshot scope |
| Sync Gmail changes | `ea_sync_gmail` | `POST /api/gmail/sync` | History API incremental only after first import; explicit authorization. A temporary TLS/proxy interruption is retried within a fixed bound; exhausted retries leave the History cursor unchanged for a safe later retry. |
| Legacy Inbox sort alias | `ea_sort_inbox` | `POST /api/inbox/sort` | Compatibility alias for daily triage; never a 50-thread total cap |
| Create Contact | `ea_create_contact` | `POST /api/contacts` | Admitted direct business customers only; a real person alone is insufficient |
| Resolve Inbox review | REST only | Sender detail + `POST /api/inbox/threads/{id}/human-review` | Contact-admission decisions resolve the sender's current non-opt-out reviews together. Reject filters the exact email durably. Existing Contacts never receive triage `content_uncertain` prompts; such threads are recorded as no-action. |
| Create Contact from Inbox form | REST only | `POST /api/inbox/threads/{id}/contact` | Completes Approve; no reply/send side effect |
| Review/revoke non-customer filter | REST only | `GET/DELETE /api/inbox/non-customer-filters[/{id}]` | Separate from outbound Suppression |
| Preview/import Contacts | `ea_import_contacts` | `POST /api/contacts/import` | Workspace CSV/XLSX only |
| Create Campaign | `ea_create_campaign` | `POST /api/campaigns` | Configuration write; an empty list is normal and never means a configuration file is missing |
| Review one Campaign | `ea_get_campaign` | `GET /api/campaigns/{id}` | Read-only |
| Review Campaign members | `ea_list_campaign_members` | `GET /api/campaigns/{id}/contacts?include_removed=true` | Read current and historical membership; no write |
| Add/re-add Campaign members | `ea_add_campaign_contacts` | `POST /api/campaigns/{id}/contacts` | Explicit configuration write; reuses historical membership |
| Remove Campaign member | `ea_remove_campaign_contact` | `DELETE /api/campaigns/{id}/contacts/{contact_id}` | Only after a direct operator removal request and an explicit destructive-action confirmation; stops future Campaign work and cancels unsent items |
| Update / start / pause / stop Campaign | `ea_update_campaign`, `ea_start_campaign`, `ea_pause_campaign`, `ea_stop_campaign` | Campaign API | Configuration/lifecycle writes; no send by themselves |
| Generate Campaign outreach | `ea_generate_campaign_outreach` | `POST /api/campaigns/{id}/generate` | Creates Drafts and pending Approvals; never report this as sent |
| Review Campaign generation | `ea_get_campaign_generation` | `GET /api/campaigns/{id}/generation-status` | Read-only; reconcile after a timeout; never starts a second generation |
| Review Automation | `ea_get_automation` | `GET /api/automation/{id}` | Read-only |
| Generate / create Automation | `ea_generate_automation_plan`, `ea_create_automation` | `POST /api/automation/generate`, `POST /api/automation` | Plan generation and configuration only |
| Enable / pause / schedule Automation | `ea_enable_automation`, `ea_pause_automation`, `ea_schedule_automation` | Automation API | Changes configuration; a later Run remains policy-gated |
| Start Automation Run | `ea_start_agent_run` | `POST /api/agent-runs` | Read global `approval_mode` first; `human_review` forces `semi_auto` |
| Confirm frozen Run | `ea_confirm_run` | `POST /api/agent-runs/{id}/confirm` | Send-capable; exact-plan confirmation |
| Cancel frozen Run | `ea_cancel_run` | `POST /api/agent-runs/{id}/cancel` | No send |
| Invalidate Approval | `ea_invalidate_approval` | `POST /api/approvals/{id}/invalidate` | No send; cancels unsent Draft |
| Add Knowledge | `ea_create_knowledge` | `POST /api/knowledge` | Published content affects future replies |
| Delete Knowledge | `ea_delete_knowledge` | `DELETE /api/knowledge/{id}` | Permanent; explicit authorization required |

## Mandatory rules

- Call the three health/status tools before a write action.
- Direct TACWork write tools require `user_authorized=true` after explicit user authorization. A valid, active Agent Takeover capability is standing authorization for its allowed operating tools, except Contact admission which always remains human-only.
- For routine Campaign and Automation work, use the typed `ea_*` tool listed above. Do not start source-code/API exploration to discover an ordinary member, Campaign, or Automation operation. If a required typed capability is unavailable, report the exact missing operation and stop before writing.
- In a scheduled Agent Takeover session, a typed-tool error is not evidence that the tool is unavailable. Do not switch to REST, Shell, source exploration, or a guessed endpoint, and do not automatically retry. Report the tool name and structured error, skip that stage, and never claim it completed. Scheduled status, Dashboard, Inbox and daily-triage reads carry the private takeover context too, so their failure is recorded as an MCP-stage error rather than being hidden by a later idle Session.
- An empty Campaign list is a normal create-ready state, not a missing Campaign configuration file. When the user has supplied complete Campaign information and explicit authorization, call `ea_create_campaign` directly.
- A timeout from `ea_generate_campaign_outreach` is **result unknown**, not proof that no Draft exists. First read `ea_get_campaign_generation`, `ea_list_approvals`, and `ea_list_campaign_members`: for `running`, wait and reconcile; for terminal `failed` or `partial`, stop and report the real result. Never retry automatically or reuse an old generation-status as a new call result. A new generate call requires new explicit user authorization plus confirmation of active + queued members and no corresponding pending Approval.
- Use `ea_generate_inbox_reply` only for a thread already admitted and eligible for reply. It creates a Draft and pending Approval in the original Gmail thread; show/review the result under the normal Approval contract before any send.
- To revise generated copy from a user instruction, first use `ea_list_approvals` and identify exactly one `pending` Approval. If the target is ambiguous, ask which Approval to revise; never infer a Campaign-wide batch. Call `ea_revise_approval` with `user_authorized=true`. It updates the existing Draft and Approval only, leaves it `pending`, and never confirms or sends. Inbox replies retain their Gmail thread Subject, `In-Reply-To`, and `References`; only first Campaign outreach may revise its subject. If the revision cannot be applied, report the failure and leave the existing content unchanged. Do not invalidate the Approval, remove/re-add the Campaign member, reset `outreach_generated`, or regenerate to recover from a revision failure.
- Before `ea_confirm_run`, show exact recipients, subject and full frozen body.
- In `human_review`, do not represent a prepared Run as sent: wait for a person to confirm the exact frozen plan. In `agent_review`, `full_auto` may dispatch after server safeguards; never use it as a harmless test.
- HTTP 200, queued/running, Draft or Approval creation is not proof of sending.
- An unsubscribe creates Suppression only for an existing Contact, verified direct human, or Campaign recipient; footer text in unknown/filtered mail is not an opt-out event.
- Contact admission always precedes sales reply and follow-up. `full_auto` does not bypass an unresolved admission decision.
- Reject is a reversible CRM/Inbox exclusion by exact sender email. It is not unsubscribe, does not create Suppression, and does not authorize any send.
- Approval Reject cancels its unsent Draft; Invalidate expires an explicitly operator-identified obsolete or already-sent pending Approval and also cancels its unsent Draft. Neither action sends email. A revision, Draft-update, timeout, or generation failure is never an invalidation reason.
- `contact_admission_uncertain` offers Approve / Reject / Agent Decide and is resolved at sender level. `content_uncertain` means special content (for example job, forwarded, or scripted content) cannot be safely classified; it creates no reply or send. For an existing Contact it is automatically recorded as no-action and never appears in the review queue. `opt_out_confirmation` remains a stop-contact decision. `stale_review` is cleared locally; none of these is an instruction for a human to reply.
- The bridge never reads or returns `.env`, model keys, OAuth tokens or Gmail credentials.
- The first mailbox import is a one-time, user-authorized local ingestion of all
  accessible mail excluding Spam/Trash. Scheduled takeover cannot start it. Once
  complete, `ea_sync_gmail` is truly incremental; `cursor_expired` requires a new
  explicit recovery decision and must never trigger a silent full rescan.
- First-history Inbox triage is a separate user-authorized background task after
  import completion. It processes only its frozen local snapshot in 50-thread
  batches, supports pause/resume/cancel/retry, and must never be started by
  scheduled takeover. It only classifies mail, filters noise and applies Contact
  admission; it never creates Drafts, Approvals or sends mail.
- Gmail OAuth setup is guided, not performed by the bridge: instruct the operator to create a Google **Desktop app** client, download `credentials.json`, then import it through **Agent Settings → Gmail Customer-Owned OAuth** and select Connect Gmail. Do not request a local path or file contents. A Web application client, Authorized JavaScript origins, and Authorized redirect URIs are not part of this product flow; tell the operator to create a new Desktop app client instead.
- Knowledge categories are customer-defined. The eight UI suggestions are optional
  organization aids, not an industry schema or a license for the Agent to infer
  missing facts. Only the current Workspace's published documents are customer
  knowledge; built-in entries are operational/safety fallback.

OpenCode starts `scripts/mcp_server.py` with bundled Python. The transition default
API is `http://127.0.0.1:18000`; Electron/launchers always provide the selected
loopback address through `EMAIL_AUTOMATION_API_URL`.
For scheduled Agent Takeover, the bridge accepts the short-lived takeover grant
for Gmail incremental sync, daily triage creation/control/monitoring, same-owner
Campaign configuration, Automation configuration, and an owned enabled Run.
Contact admission remains human-only; the grant never bypasses any server safety
gate. `logs/mcp-server.log` is the
minimal local diagnostic for bridge startup and tool/protocol errors. It must never
contain a grant, API key, OAuth credential, recipient, or email content.

The scheduled session's grant is injected through private TACWork system context,
not its user-visible prompt or report. `ea_takeover_status` is authoritative for
the current grant, health, Gmail, pause and approval mode; `ea_dashboard` is the
queue-metrics source. On a persistent conflict, scheduled takeover reports
`state_conflict` and performs no write. At the end of a cycle it refreshes
`ea_takeover_status`; a missing or past next-run timestamp means scheduler
reconciliation is pending. A finished TACWork session with an MCP-stage error is
reported as `completed_with_errors`, not `completed`.

## Diagnostics: future Agent-Native interface (reserved, not implemented)

Diagnostics are currently human-triggered only. The REST endpoints
`GET /api/system/diagnostics` and `POST /api/system/diagnostics/investigate`
are **reserved as the future Agent-Native interface**; no MCP diagnostic tools
(`ea_diagnostics_status`, `ea_investigate_diagnostics`, …) are registered in
this round, and the Agent must not call diagnostics on its own, on a schedule,
or as part of takeover monitoring. If/when Agent Native diagnosis is introduced,
it must reuse these endpoints, stay read-only, never auto-repair
(`requires_confirmation` means "a human executes it"), and render every evidence
item through the shared redaction rules.
