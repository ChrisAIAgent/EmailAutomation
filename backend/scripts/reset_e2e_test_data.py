"""Safely preview or remove E2E-only demo data from the local SQLite database.

This tool intentionally targets only Campaign names beginning with ``E2E ``.
It never touches Gmail accounts, OAuth credentials, system flags, or Campaigns
outside that prefix. Run without flags for a dry-run; applying requires an
explicit confirmation phrase.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Allow direct execution from the backend directory or the repository root.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import models
from app.db import SessionLocal, engine


CONFIRMATION = "RESET-E2E-DATA"


def _ids(rows):
    return [row[0] for row in rows]


def _count(query) -> int:
    return query.count()


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or reset E2E demo data.")
    parser.add_argument("--apply", action="store_true", help="Apply the cleanup after confirmation.")
    parser.add_argument("--confirm", default="", help=f"Required phrase: {CONFIRMATION}")
    args = parser.parse_args()

    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    if not str(engine.url).startswith("sqlite"):
        parser.error("This local-demo cleanup tool supports SQLite only.")

    db = SessionLocal()
    try:
        campaign_ids = _ids(db.query(models.Campaign.id).filter(models.Campaign.name.like("E2E %")).all())
        if not campaign_ids:
            print("No E2E campaigns found. Nothing to clean.")
            return 0

        automation_ids = _ids(
            db.query(models.Automation.id).filter(models.Automation.campaign_id.in_(campaign_ids)).all()
        )
        campaign_contact_ids = _ids(
            db.query(models.CampaignContact.id)
            .filter(models.CampaignContact.campaign_id.in_(campaign_ids))
            .all()
        )
        thread_ids = _ids(
            db.query(models.EmailThread.id).filter(models.EmailThread.campaign_id.in_(campaign_ids)).all()
        )
        contact_rows = (
            db.query(models.Contact.id, models.Contact.owner_id, models.Contact.email)
            .join(models.CampaignContact, models.CampaignContact.contact_id == models.Contact.id)
            .filter(models.CampaignContact.campaign_id.in_(campaign_ids))
            .distinct()
            .all()
        )

        summary = {
            "campaigns": len(campaign_ids),
            "automations": _count(db.query(models.Automation).filter(models.Automation.id.in_(automation_ids))) if automation_ids else 0,
            "automation_runs": _count(db.query(models.AutomationRun).filter(models.AutomationRun.automation_id.in_(automation_ids))) if automation_ids else 0,
            "campaign_contacts": len(campaign_contact_ids),
            "follow_up_tasks": _count(db.query(models.FollowUpTask).filter(models.FollowUpTask.campaign_id.in_(campaign_ids))),
            "approvals": _count(db.query(models.Approval).filter(models.Approval.campaign_id.in_(campaign_ids))),
            "threads": len(thread_ids),
            "messages": _count(db.query(models.EmailMessage).filter(models.EmailMessage.thread_id.in_(thread_ids))) if thread_ids else 0,
            "agent_runs": _count(db.query(models.AgentRun).filter(models.AgentRun.campaign_id.in_(campaign_ids))),
            "tool_executions": _count(db.query(models.ToolExecution).filter(models.ToolExecution.campaign_id.in_(campaign_ids))),
        }
        print("E2E cleanup scope:")
        for key, value in summary.items():
            print(f"  {key}: {value}")

        if not args.apply:
            print("Dry-run only. Re-run with --apply --confirm RESET-E2E-DATA to perform cleanup.")
            return 0

        database_path = Path(engine.url.database).resolve()
        backup_path = database_path.with_name(
            f"{database_path.name}.e2e-reset-{datetime.now():%Y%m%d-%H%M%S}.bak"
        )
        shutil.copy2(database_path, backup_path)

        if thread_ids:
            db.query(models.EmailMessage).filter(models.EmailMessage.thread_id.in_(thread_ids)).delete(synchronize_session=False)
        db.query(models.AgentComparison).filter(
            (models.AgentComparison.campaign_id.in_(campaign_ids)) |
            (models.AgentComparison.thread_id.in_(thread_ids) if thread_ids else False)
        ).delete(synchronize_session=False)
        db.query(models.AgentRun).filter(
            (models.AgentRun.campaign_id.in_(campaign_ids)) |
            (models.AgentRun.thread_id.in_(thread_ids) if thread_ids else False)
        ).delete(synchronize_session=False)
        db.query(models.ToolExecution).filter(models.ToolExecution.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
        db.query(models.Approval).filter(models.Approval.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
        if campaign_contact_ids:
            db.query(models.EmailDraft).filter(models.EmailDraft.campaign_contact_id.in_(campaign_contact_ids)).delete(synchronize_session=False)
        db.query(models.FollowUpTask).filter(models.FollowUpTask.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
        if automation_ids:
            db.query(models.AutomationRun).filter(models.AutomationRun.automation_id.in_(automation_ids)).delete(synchronize_session=False)
            db.query(models.Automation).filter(models.Automation.id.in_(automation_ids)).delete(synchronize_session=False)
        if campaign_contact_ids:
            db.query(models.CampaignContact).filter(models.CampaignContact.id.in_(campaign_contact_ids)).delete(synchronize_session=False)
        if thread_ids:
            db.query(models.EmailThread).filter(models.EmailThread.id.in_(thread_ids)).delete(synchronize_session=False)
        db.query(models.Campaign).filter(models.Campaign.id.in_(campaign_ids)).delete(synchronize_session=False)

        deleted_contacts = 0
        deleted_suppressions = 0
        for contact_id, owner_id, email in contact_rows:
            has_remaining_campaign = db.query(models.CampaignContact).filter_by(contact_id=contact_id).first()
            if has_remaining_campaign:
                continue
            db.query(models.Contact).filter_by(id=contact_id).delete(synchronize_session=False)
            deleted_contacts += 1
            deleted_suppressions += db.query(models.Suppression).filter_by(
                owner_id=owner_id, email=email
            ).filter(models.Suppression.source.in_(("agent", "qa", "e2e"))).delete(synchronize_session=False)

        db.commit()
        print(f"Cleanup applied. Backup: {backup_path}")
        print(f"Removed E2E-only contacts: {deleted_contacts}; related test suppressions: {deleted_suppressions}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
