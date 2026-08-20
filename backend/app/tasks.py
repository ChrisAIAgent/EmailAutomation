"""Huey background worker for Automations.

Design
------
* A persistent SQLite queue lives in ``backend/data`` (isolated from the app
  DB) so tasks survive a consumer restart.
* ``execute_automation_run`` runs ONE AutomationRun through the EXISTING
  ``run_tick`` business logic -- no new email code is written here. It performs
  an ATOMIC claim (``status: queued -> running``) so the same automation can
  never run concurrently even if two workers pick it up.
* ``scan_due_automations`` is the periodic scheduler: it finds every
  ``enabled`` Automation whose ``next_run_at`` is due and enqueues it, de-duped
  (it skips an automation that already has a queued/running run).

OpenClaw is NOT used here. This worker is the local scheduler that keeps an
Automation running automatically even with no OpenClaw connection.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from huey import SqliteHuey, crontab
from sqlalchemy import func, update as sa_update
from sqlalchemy.exc import IntegrityError

from . import models
from .consumer_status import write_consumer_status
from .db import SessionLocal
from .gmail.transport import GmailTimeoutError
from .services import automation as automation_svc
from .services import agent_takeover as takeover_svc

logger = logging.getLogger("automation.worker")

# Statuses considered "in flight": a run that is either waiting in the queue or
# actively executing. No Automation may have more than one of these at a time
# (enforced by the partial unique index uq_automation_inflight).
_INFLIGHT = ("queued", "running", "recovery_pending", "awaiting_confirmation", "confirmed")


class InflightRunExists(Exception):
    """Raised by enqueue_run(fail_if_inflight=True) when an inflight run exists.

    Callers may map this to HTTP 409 while still surfacing the existing run id.
    """

    def __init__(self, run_id: int):
        self.run_id = run_id
        super().__init__(f"automation already has an inflight run {run_id}")


def _inflight_filter(automation_id: int):
    return (
        models.AutomationRun.automation_id == automation_id,
        models.AutomationRun.status.in_(_INFLIGHT),
    )

# Queue is stored under the resolved data dir (runbook Section 4: queue/
# subdir) so it is decoupled from the app DB and persists across restarts,
# including when the program dir is read-only (installs to Program Files).
from .config import _DATA

_HUEY_DB = str(_DATA.queue / "huey.db")

huey = SqliteHuey("email_automation", filename=_HUEY_DB)


def validate_task_registry() -> None:
    """Fail fast if the consumer was started without importing all tasks.

    Huey serializes tasks by their fully-qualified name.  A consumer started
    with a stale or incomplete import path otherwise loops on ``not found in
    TaskRegistry`` and leaves AutomationRuns queued forever.
    """
    required = (
        "app.tasks.execute_automation_run",
        "app.tasks.execute_prepared_agent_run",
        "app.tasks.periodic_heartbeat",
        "app.tasks.periodic_reclaim",
        "app.tasks.periodic_scan",
    )
    missing = [name for name in required if name not in huey._registry._registry]
    if missing:
        raise RuntimeError(
            "Huey task registry incomplete; missing: " + ", ".join(missing)
        )


@huey.task()
def execute_automation_run(run_id: int, trigger: str, source: str) -> dict:
    """Execute one queued AutomationRun. Runs in the consumer process."""
    db = SessionLocal()
    try:
        run = db.get(models.AutomationRun, run_id)
        if run is None:
            return {"ok": False, "error": "run not found"}

        # Atomic claim: only the first worker to flip queued->running proceeds.
        # A second concurrent execution finds status != queued and bails out,
        # guaranteeing no duplicate work for the same Automation.
        res = db.execute(
            sa_update(models.AutomationRun)
            .where(
                models.AutomationRun.id == run_id,
                models.AutomationRun.status == "queued",
            )
            .values(status="running", started_at=func.now())
        )
        db.commit()
        if res.rowcount == 0:
            return {"ok": False, "skipped": True, "reason": "not_queued"}

        run = db.get(models.AutomationRun, run_id)
        automation = db.get(models.Automation, run.automation_id)
        if automation is None:
            run.status = "failed"
            run.error = "automation not found"
            run.finished_at = func.now()
            db.commit()
            return {"ok": False, "error": "automation not found"}

        try:
            summary = automation_svc.run_tick(
                db, automation, trigger=trigger, source=source, run=run
            )
            run.finished_at = func.now()
            db.commit()
            return {"ok": True, **summary}
        except GmailTimeoutError as e:
            # Real Gmail network timeout. The run MUST reach a terminal state
            # (failed) with a recognizable gmail_timeout reason, and THIS worker
            # thread truly returns -- no background continuation, so a fresh run
            # for the same Automation can be picked up without concurrency.
            db.rollback()
            run = db.get(models.AutomationRun, run_id)
            run.status = "failed"
            run.error = str(e)[:2000]
            run.finished_at = func.now()
            db.commit()
            logger.warning("automation run %s failed (gmail timeout): %s", run_id, e)
            return {"ok": False, "error": str(e)[:2000]}
        except Exception as e:  # pragma: no cover - defensive
            db.rollback()
            run = db.get(models.AutomationRun, run_id)
            run.status = "failed"
            run.error = str(e)[:2000]
            run.finished_at = func.now()
            db.commit()
            logger.exception("automation run %s failed: %s", run_id, e)
            return {"ok": False, "error": str(e)[:2000]}
    finally:
        db.close()


@huey.task()
def execute_prepared_agent_run(run_id: int) -> dict:
    """Dispatch a frozen semi-auto plan after its single user confirmation."""
    db = SessionLocal()
    try:
        run = db.get(models.AutomationRun, run_id)
        if run is None:
            return {"ok": False, "error": "run not found"}
        claimed = db.execute(
            sa_update(models.AutomationRun)
            .where(models.AutomationRun.id == run_id,
                   models.AutomationRun.status == "confirmed")
            .values(status="running", started_at=func.now())
        )
        db.commit()
        if claimed.rowcount == 0:
            return {"ok": False, "skipped": True, "reason": "not_confirmed"}
        run = db.get(models.AutomationRun, run_id)
        try:
            summary = automation_svc.dispatch_prepared_run(
                db, run, actor=run.confirmed_by or "user:confirmed"
            )
            run.finished_at = func.now()
            automation = db.get(models.Automation, run.automation_id)
            if automation:
                automation.last_run_at = datetime.now(timezone.utc)
                automation.last_status = run.status
            db.commit()
            return {"ok": True, "run_id": run.id, **summary}
        except Exception as e:  # pragma: no cover - defensive transport boundary
            db.rollback()
            run = db.get(models.AutomationRun, run_id)
            run.status = "failed"
            run.error = str(e)[:2000]
            run.finished_at = func.now()
            db.commit()
            logger.exception("prepared agent run %s failed: %s", run_id, e)
            return {"ok": False, "error": str(e)[:2000]}
    finally:
        db.close()


def enqueue_run(db, automation, trigger: str, source: str, fail_if_inflight: bool = False,
                execution_mode: str | None = None) -> models.AutomationRun:
    """Create a QUEUED AutomationRun and enqueue it, enforcing ONE inflight run
    per Automation at the database layer.

    Guarantees (requirement: never more than one queued/running run, even under
    concurrent/rapid Run-now clicks):

      * If an inflight (queued/running) run already exists, return it (idempotent)
        unless ``fail_if_inflight=True`` -- then raise ``InflightRunExists`` so the
        API can answer 409 with the existing run id.
      * Otherwise INSERT a new queued run. The partial unique index
        ``uq_automation_inflight`` makes the insert atomic: if a concurrent request
        won the race, our INSERT raises IntegrityError, we roll back and return the
        winner. So we never rely on a fragile SELECT-then-INSERT alone.

    Commits the row so the (separate) consumer process can read it, then returns
    immediately. Safe to call from a request handler.
    """
    existing = db.query(models.AutomationRun).filter(*_inflight_filter(automation.id)).first()
    if existing is not None:
        if fail_if_inflight:
            raise InflightRunExists(existing.id)
        return existing

    from .services.agent_profile import get_or_create_profile
    requested_mode = execution_mode or automation.execution_mode or "full_auto"
    profile = get_or_create_profile(db, automation.owner_id)
    effective_mode = "semi_auto" if (profile.approval_mode or "human_review") == "human_review" else requested_mode

    run = models.AutomationRun(
        automation_id=automation.id, trigger=trigger, source=source, status="queued",
        execution_mode=effective_mode,
    )
    db.add(run)
    try:
        db.flush()  # populate run.id
        run_id = run.id
        # Persist now so the consumer process (separate DB connection) can read it.
        db.commit()
    except IntegrityError:
        # A concurrent request inserted an inflight run between our check and our
        # INSERT. The unique index blocked us -- fold into the winner.
        db.rollback()
        winner = db.query(models.AutomationRun).filter(*_inflight_filter(automation.id)).first()
        if winner is not None:
            return winner
        raise
    # In Huey 3.x calling the task enqueues it (returns immediately). The
    # consumer process later picks it up and runs the function body.
    execute_automation_run(run_id, trigger, source)
    return run


def scan_due_automations(owner_id: int | None = None) -> int:
    """Enqueue every enabled Automation whose next_run_at is due (de-duped).

    IMPORTANT (no-drift rule): when an Automation already has an inflight run we
    SKIP it WITHOUT advancing ``next_run_at``. The schedule is only advanced when
    we actually create a task (here) or when the run completes (in run_tick). This
    prevents the per-minute scan from indefinitely pushing the execution time
    forward while a run is still in flight.

    Returns the number of Automations enqueued this pass.
    """
    db = SessionLocal()
    enqueued = 0
    try:
        now = datetime.now(timezone.utc)
        query = (
            db.query(models.Automation)
            .filter_by(status="enabled")
            .filter(models.Automation.next_run_at <= now)
        )
        if owner_id is not None:
            query = query.filter(models.Automation.owner_id == owner_id)
        due = query.all()
        for a in due:
            # TACWork exclusively owns Global Inbox cadence and creates a
            # fresh operating session. The legacy scanner must never run that
            # scope directly, even while takeover is off; Campaign automation
            # retains its existing direct Huey behavior.
            if (a.scope or "campaign") == "global":
                a.next_run_at = None
                db.commit()
                continue
            # Inflight exists -> skip WITHOUT advancing the schedule (no drift).
            inflight = (
                db.query(models.AutomationRun)
                .filter(*_inflight_filter(a.id))
                .first()
            )
            if inflight:
                continue
            # No inflight -> create the task and advance the schedule ONLY now.
            enqueue_run(db, a, trigger="cron", source="backend")
            a.next_run_at = now + timedelta(minutes=a.tick_interval_minutes)
            db.add(a)
            db.commit()
            enqueued += 1
        return enqueued
    finally:
        db.close()


def reclaim_stale_runs(timeout_minutes: int | None = None) -> int:
    """Reclaim runs stuck in ``running`` past the timeout as ``failed``.

    A run is "stale" when it has been running longer than ``timeout_minutes``
    (default: Settings.STALE_RUN_TIMEOUT_MINUTES). Causes include a consumer crash
    mid-execution or an unreachable external dependency (e.g. Gmail sync). Such a
    run is marked ``failed`` with ``error='worker_timeout'`` and a ``finished_at``
    timestamp, so the Automation is free to run again on the next scan.

    The UPDATE is atomic (``WHERE status='running' AND started_at < cutoff``) so
    concurrent reclaimers or a restarted consumer cannot double-process the same
    row. Called at consumer startup and periodically, so a crashed consumer
    recovers its own stuck runs.

    Returns the number of runs reclaimed.
    """
    from .config import get_settings

    if timeout_minutes is None:
        timeout_minutes = get_settings().STALE_RUN_TIMEOUT_MINUTES
    # SQLite stores tz-aware timestamps as naive UTC; compare in naive UTC.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=timeout_minutes)
    for attempt_no in range(3):
        db = SessionLocal()
        try:
            stale = db.query(models.AutomationRun).filter(
                models.AutomationRun.status == "running",
                models.AutomationRun.started_at.isnot(None),
                models.AutomationRun.started_at < cutoff,
            ).all()
            for run in stale:
                uncertain = (
                    db.query(models.DeliveryAttempt)
                    .join(models.Approval, models.Approval.id == models.DeliveryAttempt.approval_id)
                    .filter(
                        models.Approval.automation_run_id == run.id,
                        models.DeliveryAttempt.status.in_(("sending", "unknown", "gmail_sent")),
                    ).count()
                )
                run.status = "recovery_pending" if uncertain else "failed"
                run.error = (
                    "worker_timeout_reconciliation_required"
                    if uncertain else "worker_timeout"
                )
                run.finished_at = func.now()
            db.commit()
            return len(stale)
        except Exception as exc:
            db.rollback()
            if "database is locked" not in str(exc).lower() or attempt_no == 2:
                raise
            time.sleep(0.2 * (attempt_no + 1))
        finally:
            db.close()
    return 0


@huey.periodic_task(crontab(minute="*"))
def periodic_heartbeat():
    """Prove that the Huey scheduler and a worker can execute tasks."""
    write_consumer_status("running")


@huey.periodic_task(crontab(minute="*"))
def periodic_reclaim():
    """Periodically reclaim stale running runs so Automations never wedge."""
    try:
        n = reclaim_stale_runs()
        if n:
            logger.info("reclaimed %s stale running run(s)", n)
    except Exception as e:  # pragma: no cover - logging only
        logger.exception("periodic reclaim failed: %s", e)


@huey.periodic_task(crontab(minute="*"))
def periodic_scan():
    """Run the scheduler every minute.

    The 5-minute cadence is governed by each Automation's ``next_run_at``
    (set on enable and advanced by run_tick after every run), not by this
    crontab. This just checks frequently enough to catch due automations.
    """
    try:
        n = scan_due_automations()
        if n:
            logger.info("periodic scan enqueued %s automation(s)", n)
    except Exception as e:  # pragma: no cover - logging only
        logger.exception("periodic scan failed: %s", e)


@huey.periodic_task(crontab(minute="*"))
def periodic_agent_takeover():
    """Wake one fresh TACWork operating session when Agent Takeover is due."""
    db = SessionLocal()
    try:
        result = takeover_svc.trigger_due(db)
        if result.get("status") not in {"disabled", "not_due"}:
            logger.info("agent takeover scheduler result: %s", result)
    except Exception as exc:  # pragma: no cover - logging only
        db.rollback()
        logger.exception("agent takeover scheduler failed: %s", exc)
    finally:
        db.close()
