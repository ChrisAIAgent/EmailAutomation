"""Huey consumer entry point for the Automation scheduler.

Starts the background worker that:
  * runs the periodic scan (every minute) to enqueue due, enabled Automations,
  * executes queued AutomationRuns through the existing run_tick business logic.

Run from the backend directory:

    python run_consumer.py

(Equivalent to: huey_consumer app.tasks.huey -w 2 -C)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.db import init_db
from app.consumer_status import write_consumer_status
from app.tasks import huey, validate_task_registry
from app.config import _DATA


def _acquire_single_instance_lock():
    """Hold an OS file lock for the lifetime of this consumer process."""
    lock_path = _DATA.queue / "consumer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - Windows is the supported Demo platform.
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            f"another Huey consumer already owns {lock_path}"
        ) from exc
    return handle


def main() -> None:
    lock_handle = _acquire_single_instance_lock()
    # Import-time registration must be complete before Huey starts reading the
    # persistent queue. This prevents a bad consumer process from silently
    # leaving queued AutomationRuns behind.
    validate_task_registry()
    # Ensure all tables exist; the worker reads/writes via the same ORM.
    init_db()
    # Recover any runs left in `running` by a previous (crashed) consumer or by
    # an unreachable external dependency. This must run before the worker starts
    # so a stale run does not block its Automation forever.
    from app.tasks import reclaim_stale_runs

    try:
        n = reclaim_stale_runs()
        if n:
            print(f"[consumer] reclaimed {n} stale running run(s) as failed")
    except Exception as e:  # pragma: no cover - logging only
        print(f"[consumer] reclaim on startup failed: {e}")

    from huey.consumer import Consumer

    write_consumer_status("running")
    print(f"[consumer] started pid={os.getpid()} workers=2 periodic=true", flush=True)
    consumer = Consumer(huey, workers=2, periodic=True)
    try:
        consumer.run()
    except Exception as exc:
        write_consumer_status("failed", str(exc)[:300])
        raise
    finally:
        write_consumer_status("stopped")
        # Keep the handle alive until shutdown; closing releases the OS lock.
        lock_handle.close()


if __name__ == "__main__":
    main()
