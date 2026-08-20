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
| Check Gmail account | `ea_gmail_status` | `GET /api/gmail/status` | Read-only; no tokens |
| Check global pause | `ea_system_pause` | `GET /api/system/pause` | Read-only |
| Review Contacts | `ea_list_contacts` | `GET /api/contacts` | Read-only |
| Review Campaigns | `ea_list_campaigns` | `GET /api/campaigns` | Read-only |
| Review Approval mode | `ea_get_profile` | `GET /api/agent-profile?create_if_missing=false` | Read `approval_mode` before any send-capable task |
| Set Approval mode | `ea_set_approval_mode` | `PUT /api/agent-profile/approval-mode` | High-impact write; requires `user_authorized=true` |
| Review Approvals | `ea_list_approvals` | `GET /api/approvals` | Read-only |
| Review Automations | `ea_list_automations` | `GET /api/automation` | Read-only |
| Review operating state | `ea_dashboard` | readiness, metrics, Inbox stats, scheduler | Read-only |
| Review Inbox queue | `ea_list_inbox` | `GET /api/inbox/threads` | `contact_admission_uncertain` is Contact admission; `content_uncertain` requires human message-context review |
| Review Agent Run | `ea_get_agent_run` | `GET /api/agent-runs/{id}` | Read-only |
| Generate live report | `ea_agent_report` | current read endpoints | Read-only aggregation |
| Sync Gmail | `ea_sync_gmail` | `POST /api/gmail/sync` | Writes local data; explicit authorization |
| Create Contact | `ea_create_contact` | `POST /api/contacts` | Admitted direct business customers only; a real person alone is insufficient |
| Resolve Inbox review | REST only | `POST /api/inbox/threads/{id}/human-review` | For Contact admission: Approve opens manual entry, Reject filters exact email, Agent Decide needs clear identity and business relevance. For an existing Contact's `content_uncertain`, only explicit no-action confirmation is valid. |
| Create Contact from Inbox form | REST only | `POST /api/inbox/threads/{id}/contact` | Completes Approve; no reply/send side effect |
| Review/revoke non-customer filter | REST only | `GET/DELETE /api/inbox/non-customer-filters[/{id}]` | Separate from outbound Suppression |
| Preview/import Contacts | `ea_import_contacts` | `POST /api/contacts/import` | Workspace CSV/XLSX only |
| Create Campaign | `ea_create_campaign` | `POST /api/campaigns` | Configuration write; no send |
| Review Campaign members | REST only | `GET /api/campaigns/{id}/contacts?include_removed=true` | Read current and historical membership; no write |
| Add/re-add Campaign members | REST only | `POST /api/campaigns/{id}/contacts` | Explicit configuration write; reuses historical membership |
| Remove Campaign member | REST only | `DELETE /api/campaigns/{id}/contacts/{contact_id}` | Stops future Campaign work, preserves history, cancels unsent items; never creates Suppression |
| Start Automation Run | `ea_start_agent_run` | `POST /api/agent-runs` | Read global `approval_mode` first; `human_review` forces `semi_auto` |
| Confirm frozen Run | `ea_confirm_run` | `POST /api/agent-runs/{id}/confirm` | Send-capable; exact-plan confirmation |
| Cancel frozen Run | `ea_cancel_run` | `POST /api/agent-runs/{id}/cancel` | No send |
| Invalidate Approval | `ea_invalidate_approval` | `POST /api/approvals/{id}/invalidate` | No send; cancels unsent Draft |
| Add Knowledge | `ea_create_knowledge` | `POST /api/knowledge` | Published content affects future replies |
| Delete Knowledge | `ea_delete_knowledge` | `DELETE /api/knowledge/{id}` | Permanent; explicit authorization required |

## Mandatory rules

- Call the three health/status tools before a write action.
- Write tools require `user_authorized=true` after explicit user authorization.
- Before `ea_confirm_run`, show exact recipients, subject and full frozen body.
- In `human_review`, do not represent a prepared Run as sent: wait for a person to confirm the exact frozen plan. In `agent_review`, `full_auto` may dispatch after server safeguards; never use it as a harmless test.
- HTTP 200, queued/running, Draft or Approval creation is not proof of sending.
- An unsubscribe creates Suppression only for an existing Contact, verified direct human, or Campaign recipient; footer text in unknown/filtered mail is not an opt-out event.
- Contact admission always precedes sales reply and follow-up. `full_auto` does not bypass an unresolved admission decision.
- Reject is a reversible CRM/Inbox exclusion by exact sender email. It is not unsubscribe, does not create Suppression, and does not authorize any send.
- Approval Reject cancels its unsent Draft; Invalidate expires an obsolete or already-sent pending Approval and also cancels its unsent Draft. Neither action sends email.
- `contact_admission_uncertain` offers Approve / Reject / Agent Decide. `content_uncertain` means special content (for example job, forwarded, or scripted content) cannot be safely classified; it creates no reply or send. For an existing Contact, the operator may confirm no action while keeping the Contact. `opt_out_confirmation` is a stop-contact decision. `stale_review` is cleared locally; none of these is an instruction for a human to reply.
- The bridge never reads or returns `.env`, model keys, OAuth tokens or Gmail credentials.
- Knowledge categories are customer-defined. The eight UI suggestions are optional
  organization aids, not an industry schema or a license for the Agent to infer
  missing facts. Only the current Workspace's published documents are customer
  knowledge; built-in entries are operational/safety fallback.

OpenCode starts `scripts/mcp_server.py` with bundled Python. The default API is
`http://127.0.0.1:8000`; `EMAIL_AUTOMATION_API_URL` is an optional local override.
For scheduled Agent Takeover, the bridge accepts the short-lived takeover grant
only for Gmail sync, Inbox sort, the owner's Global Inbox Run, and its Run poll;
it does not authorize Campaign/Profile/Knowledge writes, deletion, Contact
admission, or bypassing any server safety gate. `logs/mcp-server.log` is the
minimal local diagnostic for bridge startup and tool/protocol errors. It must never
contain a grant, API key, OAuth credential, recipient, or email content.
