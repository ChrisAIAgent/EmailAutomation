"""Back up the live SQLite DB and remove legacy Approvals without a Draft."""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import models
from app.db import SessionLocal


def main() -> None:
    db_path = BACKEND_DIR / "app.db"
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"app.db.bak-empty-approvals-{stamp}")
    shutil.copy2(db_path, backup_path)

    db = SessionLocal()
    try:
        rows = (
            db.query(models.Approval)
            .filter(models.Approval.draft_id.is_(None))
            .all()
        )
        ids = [row.id for row in rows]
        for row in rows:
            db.delete(row)
        db.commit()
        remaining = (
            db.query(models.Approval)
            .filter(models.Approval.draft_id.is_(None))
            .count()
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(f"backup={backup_path}")
    print(f"deleted={len(ids)} ids={ids}")
    print(f"remaining_empty={remaining}")


if __name__ == "__main__":
    main()
