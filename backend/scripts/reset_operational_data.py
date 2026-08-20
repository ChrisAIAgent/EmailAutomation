"""Reset operational data while preserving identity and Gmail OAuth.

This is the handoff reset used before a new Agent takes ownership of the
workspace. It supports only the local SQLite deployment and always creates a
database backup before deleting anything.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import models
from app.db import SessionLocal, engine


CONFIRMATION = "RESET-OPERATIONAL-DATA"

# Child tables must be deleted before their parents. User, GmailAccount and
# OAuthCredential are intentionally absent from this list.
RESET_MODELS = (
    models.AutomationRun,
    models.AgentComparison,
    models.AgentRun,
    models.ToolExecution,
    models.AuditLog,
    models.Approval,
    models.EmailDraft,
    models.FollowUpTask,
    models.EmailMessage,
    models.EmailThread,
    models.Automation,
    models.CampaignContact,
    models.Campaign,
    models.Suppression,
    models.Contact,
    models.SystemFlag,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset all operational data while preserving Gmail OAuth."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    if not str(engine.url).startswith("sqlite"):
        parser.error("This reset supports the local SQLite deployment only.")

    db = SessionLocal()
    try:
        oauth_account_ids = {
            row[0]
            for row in db.query(models.OAuthCredential.gmail_account_id).all()
        }
        if not oauth_account_ids:
            raise RuntimeError(
                "No OAuth-backed Gmail account exists; refusing operational reset."
            )
        stale_gmail_accounts = (
            db.query(models.GmailAccount)
            .filter(~models.GmailAccount.id.in_(oauth_account_ids))
            .count()
        )
        before = {
            model.__tablename__: db.query(model).count()
            for model in RESET_MODELS
        }
        preserved = {
            "users": db.query(models.User).count(),
            "oauth_backed_gmail_accounts": len(oauth_account_ids),
            "oauth_credentials": db.query(models.OAuthCredential).count(),
        }
        print("Operational reset scope:")
        for name, count in before.items():
            print(f"  delete {name}: {count}")
        print(f"  delete gmail_accounts_without_oauth: {stale_gmail_accounts}")
        for name, count in preserved.items():
            print(f"  preserve {name}: {count}")

        if not args.apply:
            print(
                "Dry-run only. Re-run with --apply "
                f"--confirm {CONFIRMATION} to perform the reset."
            )
            return 0

        database_path = Path(engine.url.database).resolve()
        backup_path = database_path.with_name(
            f"{database_path.name}.operational-reset-{datetime.now():%Y%m%d-%H%M%S}.bak"
        )
        shutil.copy2(database_path, backup_path)

        for model in RESET_MODELS:
            db.query(model).delete(synchronize_session=False)
        db.query(models.GmailAccount).filter(
            ~models.GmailAccount.id.in_(oauth_account_ids)
        ).delete(synchronize_session=False)
        db.query(models.GmailAccount).filter(
            models.GmailAccount.id.in_(oauth_account_ids)
        ).update(
            {models.GmailAccount.history_id: None},
            synchronize_session=False,
        )
        db.commit()

        remaining = sum(db.query(model).count() for model in RESET_MODELS)
        print(f"Cleanup applied. Backup: {backup_path}")
        print(f"Remaining operational rows: {remaining}")
        print(
            "Preserved: "
            f"users={db.query(models.User).count()}, "
            f"gmail_accounts={db.query(models.GmailAccount).count()}, "
            f"oauth_credentials={db.query(models.OAuthCredential).count()}"
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
