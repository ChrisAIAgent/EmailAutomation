# Email Automation — Gmail Outreach Agent

A self-hosted email outreach workspace: synchronise a Gmail inbox, classify
incoming mail, build outreach Campaigns, generate draft-only prospecting emails
with an LLM, route replies through an approval workflow, and run automated
follow-ups — all behind a local web UI and a policy engine that enforces
suppression, send-windows, daily caps, and stop-rules.

> ⚠️ **Responsible use.** This tool is built for permission-based,
> legitimately-interested outreach only. Do **not** use it to send spam.
> You are responsible for complying with CAN-SPAM, GDPR, CASL, and every
> other applicable law in the jurisdictions you email. Real sending is gated
> behind `ENABLE_REAL_SEND=false` by default and a recipient allow-list; it
> must stay off until you have verified consent, human confirmation, and a
> real Gmail connection.

---

## Features

- **Gmail OAuth sync** — incremental thread/label sync with MIME decoding
  (RFC 2047 subjects, base64url, charset repair).
- **Inbound triage** — direction gating, ad/spam/system-noise filtering, and
  human-vs-bot classification before anything touches your CRM.
- **Contact admission** — a review gate decides Approve / Reject / Agent-decide
  per sender; only confirmed real-business people enter Contacts.
- **Campaigns** — import CSV contacts, select members, generate first-touch
  emails with an LLM (draft-only; human approval required before send).
- **Approval workflow** — every outbound message is an `Approval` that a human
  reviews and confirms; nothing is auto-sent without policy clearance.
- **Automation** — schedule follow-ups / reply generation; the policy engine
  stops on opt-out, bounce, or unsubscribe and respects send-windows + caps.
- **Local Agent (optional)** — an embedded agent surface for operator takeover
  and scheduled operations (the TACWork runtime is a separate project and is
  **not** bundled in this repository).
- **Observability** — dashboard readiness, metrics, inbox, scheduler status,
  and audit logs for every contact/label/approval change.

## Architecture

| Component | Stack | Role |
|-----------|-------|------|
| Backend API | FastAPI + SQLAlchemy 2.0 + Pydantic v2 | REST API, Gmail sync, policy engine, approvals |
| Worker | Huey (consumer) | Async automation ticks, follow-up generation |
| Frontend | Next.js 14 + TypeScript + Tailwind | Operator dashboard |
| Agent (optional) | MCP server (`scripts/mcp_server.py`) | Tool surface for the embedded agent |

```
browser ──► Next.js (3000) ──► FastAPI (8000) ──► Gmail API
                                  │
                                  └─► Huey consumer ──► LLM (Volcano Ark / OpenAI)
```

## Tech stack

Python 3.11+, Node 18+, SQLite (swap to PostgreSQL via `DATABASE_URL`),
Volcano Ark / OpenAI-compatible LLM, Google Gmail API (OAuth 2.0).

## Quick start

### Prerequisites

- Python 3.11+ and Node 18+ on your `PATH`
- A Google Cloud OAuth **Web application** credential
  (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, redirect URI
  `http://localhost:8000/api/gmail/oauth/callback`)
- An LLM API key (Volcano Ark or OpenAI-compatible)

### 1. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then fill in GOOGLE_*, LLM_*, SECRET_KEY, APP_ENCRYPTION_KEY
uvicorn app.main:app --reload --port 8000
```

Generate the encryption key once:

```bash
python -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

### 2. Worker (Huey consumer)

```bash
cd backend
huey_consumer app.huey:huey -w 2
```

### 3. Frontend

```bash
cd frontend
npm install
cp .env.local.example .env.local   # or set NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
npm run dev                        # http://127.0.0.1:3000
```

### 4. Or use the bundled dev launcher (Windows)

```powershell
.\start-stack.bat      # stops old processes, starts backend + consumer + frontend
.\stop-stack.bat       # stop everything
```

Operational health checks live in `scripts/agent-health.ps1`,
`scripts/agent-tick.ps1`, and `scripts/agent-report.ps1`.

## Configuration

Copy `backend/.env.example` → `backend/.env`. Key variables:

| Variable | Purpose |
|----------|---------|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Gmail OAuth credential |
| `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | LLM endpoint |
| `ENABLE_REAL_SEND` | **false** by default; turn on only after verification |
| `TEST_RECIPIENT_ALLOWLIST` | Restrict real sends to specific addresses |
| `APP_ENCRYPTION_KEY` / `SECRET_KEY` | Encrypt OAuth tokens / sign sessions |
| `DEFAULT_TIMEZONE` | Used for send-windows |

## Project layout

```
backend/        FastAPI app, policy engine, Gmail sync, Huey tasks, tests
frontend/       Next.js operator dashboard
scripts/        dev launchers, health/tick/report, MCP server, secret-scan
docs/           agent operations / capabilities / cron reference
AGENTS.md       operating contract for the embedded agent
```

## Development & tests

```bash
cd backend && pytest                 # backend test suite
cd frontend && npm run typecheck     # TypeScript type check
```

Run the secret scanner before any commit/push:

```powershell
pwsh scripts/secret-scan.ps1
```

## Distribution

This repository is **source-only**. No prebuilt installer (`.exe`) or other binary
release assets are published for this project — neither in the repository nor as
GitHub Releases. To run it, follow the Quick start above. The optional Windows
installer is built from separate packaging tooling that is intentionally not
included in this source tree.

## License

[MIT](./LICENSE) © 2026 ChrisAIAgent.

## Disclaimer

This software is provided "as is", without warranty. The authors are not
responsible for how you use it. Make sure you have the legal right to contact
every recipient and honour unsubscribe/opt-out requests immediately.
