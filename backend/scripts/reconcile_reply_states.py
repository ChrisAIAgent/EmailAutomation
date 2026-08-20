"""One-off repair: re-derive every Contact's reply state from live thread direction.

Run after deploying the single-source-of-truth fix for "needs reply" so any
Contact whose cached lifecycle_stage/next_action drifted from reality (e.g. an
outbound reply was pulled in by sync but never re-analyzed) is corrected.

Usage (from backend/ with the project venv active):
    .venv/Scripts/python.exe scripts/reconcile_reply_states.py

Read-only unless a drift is found; it only writes the corrected reply state.
"""
from __future__ import annotations

import os
import sys

# Make the backend package importable when run as a standalone script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal  # noqa: E402
from app import models  # noqa: E402
from app.services.inbox_triage import recompute_contact_reply_state  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        contacts = db.query(models.Contact).all()
        changed = 0
        print(f"Scanning {len(contacts)} contacts for reply-state drift...\n")
        for c in contacts:
            before = (c.lifecycle_stage, c.next_action)
            if recompute_contact_reply_state(db, c):
                after = (c.lifecycle_stage, c.next_action)
                print(
                    f"  [FIX] contact {c.id} {c.email}: "
                    f"{before[0]}/{before[1]} -> {after[0]}/{after[1]}"
                )
                changed += 1
        if changed:
            db.commit()
            print(f"\nCommitted {changed} correction(s).")
        else:
            print("\nNo drift found; all contact reply states already consistent.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
