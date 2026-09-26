# Mainline safety fixes: validation and hands-on acceptance

This checklist covers the three fixes in `codex/mainline-safety-fixes`. It separates
automated regression checks from the operator's UI acceptance. Run all hands-on
checks from the isolated development stack; do not use the customer installation or
its data directory.

## What must pass

### A. Locked Contact stop behavior

Automated regression must start with a manually locked Contact that has CRM-owned
category, intent, tags and segments, an active Campaign membership, one pending
Approval/Draft, one scheduled follow-up, and historical approved/sent records. Apply
a confirmed bounce and verify:

- the Contact is marked `bounced + stopped`, its next action/follow-up is cleared,
  and its Campaign membership becomes inactive;
- the manually maintained category, intent, tags, segments and `manual_lock` remain
  unchanged;
- suppression is present, pending Approval/Draft and scheduled follow-up are
  expired/cancelled, and approved/sent history is preserved;
- repeating the same bounce keeps one suppression record and does not report a
  second stop transition;
- the existing unsubscribe human-confirmation tests still pass;
- a failed stop is reported as an error and does not increase `replies_stopped`.

Do not create a real bounce by sending mail to a customer. The fixture test exercises
the exact persisted state safely.

### B. Automation create, enable, and retry

Automated API checks must submit a plan with `enabled=true` and verify that creation
returns `status=disabled`, `plan.enabled=false`, `next_run_at=null`, no queued Run,
and no scheduled entry. A separate enable call must set `status=enabled`,
`plan.enabled=true`, and a non-null `next_run_at`. Pausing must clear the due time.
Creating another Automation must not change existing Automations.

In the UI, use a development Campaign and choose **Save and enable**. Confirm there
is exactly one Automation, it ends enabled, and it has a next due time. The failure
path is covered with an automated API-mock/component check: create succeeds, enable
fails, the saved ID remains visible, and retry calls enable for that same ID without
calling create again.

### C. Long-running and persistent Run

The automated UI/API checks must exercise a Run that remains `queued` or `running`
longer than one poll interval, then reaches a stable state. The UI must use the
effective server mode (including `human_review` forcing `semi_auto`), prevent double
confirm/cancel clicks, display the full frozen plan, and expose a retry after a
status-read failure. Reopening Automation details must recover the current Run.

For hands-on persistence acceptance:

1. Start the isolated development stack and confirm the Health page reports the dev
   workspace. Configure a harmless development Campaign with one fixture Contact;
   use a connected development mailbox only if Draft creation is part of the check.
2. Start a Campaign Automation Run in semi-auto and wait for `awaiting_confirmation`.
   Record its Run ID, recipient, subject and full body. Do not confirm it.
3. Navigate away from Automation and return. The same Run ID and unchanged frozen
   content must reappear. Use refresh/status reads as needed; do not click Run now a
   second time.
4. Stop and restart the entire development stack using its normal scripts while
   keeping its development data directory. Reopen the same Automation. The same
   `awaiting_confirmation` Run and frozen plan must still be present.
5. Cancel that Run. Verify it is cancelled, no second Run or duplicate Approval was
   created, and the Gmail Sent folder has no corresponding outbound message.

This tests persistence at two levels: Huey's pending work is stored in its SQLite
queue, and Run/frozen-plan state is stored in the application database. A Consumer
restart must use the same development data directory and resume the queued work;
restarting the full stack must not replace either database. The automated Huey test
checks that a newly opened queue instance can read the pending item from the same
SQLite file. The hands-on restart checks the visible Run recovery path.

## Commands

Use a new temporary root so tests cannot open or modify the normal development or
customer database/queue. Run from `backend/` in PowerShell:

```powershell
$testRoot = Join-Path $env:TEMP ("email-automation-safety-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$env:EMAIL_AUTOMATION_DATA_DIR = Join-Path $testRoot "data"
$dbPath = (Join-Path $testRoot "app.db") -replace "\\", "/"
$env:DATABASE_URL = "sqlite:///$dbPath"
$env:ENABLE_REAL_SEND = "false"
$env:ENABLE_SCHEDULER = "false"
& .\.venv\Scripts\python.exe -m pytest tests/test_followup.py tests/test_automation.py
```

Then run the broader backend regression suite in the same isolated process
environment, followed by the component test and typecheck from `frontend/`.
The normal `npm run build` may fail if a previous Windows standalone build left
directory symlinks under `.next-prod/standalone/node_modules`; use a fresh output
directory for a verification build:

```powershell
..\tools\node\npm.cmd run test:components
..\tools\node\npm.cmd run typecheck
$env:NEXT_DIST_DIR = ".next-mainline-check-$([guid]::NewGuid().ToString('N'))"
..\tools\node\node.exe .\node_modules\next\dist\bin\next build
```

Next.js may add that one-off output directory to `tsconfig.json`; remove only that
generated include line after the build. Verification outputs matching
`.next-mainline-check*/` are ignored by Git. These checks do not replace the
hands-on restart acceptance above.

## Acceptance record

Record the commit IDs tested, test command and pass/fail totals, typecheck/build
results, development Run ID before/after restart, final cancellation state, and
whether any outbound message appeared. Mark manual acceptance complete only after
all three sections pass. Merge this branch into `main` only after that acceptance.

### Automated verification performed

- Isolated full backend suite: **359 passed**, with two dependency/test warnings.
- Frontend component workflow tests: **6 passed**.
- Frontend TypeScript typecheck: **passed**.
- Next.js production compilation: **passed** using a fresh `NEXT_DIST_DIR`.
- The standard `.next-prod` build attempt encountered `EPERM` while cleaning a
  prior standalone output containing a React directory symlink. No customer data
  was involved; the isolated-output build completed successfully.
- Operator UI restart acceptance: **pending**.
