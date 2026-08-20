"""Lightweight scheduler loop (dev). Production: swap for Celery/RQ/BullMQ."""
from __future__ import annotations

import logging
import threading
import time

from ..db import SessionLocal
from ..agents.orchestrator import Orchestrator
from ..services import followup as followup_svc
from ..events import publish

logger = logging.getLogger("scheduler")

_stop = threading.Event()


def _tick():
    from ..services import flags as flag_svc
    db = SessionLocal()
    try:
        if flag_svc.is_globally_paused(db):
            return
        orch = Orchestrator(db)
        outcomes = followup_svc.process_due_follow_ups(db, orch)
        db.commit()
        for o in outcomes:
            publish("followup", o)
    except Exception as e:
        logger.exception("scheduler tick error: %s", e)
        db.rollback()
    finally:
        db.close()


def scheduler_loop(interval_seconds: int = 60):
    logger.info("scheduler started (interval=%ss)", interval_seconds)
    while not _stop.is_set():
        _tick()
        _stop.wait(interval_seconds)


def start(interval_seconds: int = 60):
    t = threading.Thread(target=scheduler_loop, args=(interval_seconds,), daemon=True)
    t.start()
    return t


def stop():
    _stop.set()
