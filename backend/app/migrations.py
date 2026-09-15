"""Idempotent additive schema migration for the dev SQLite database.

``Base.metadata.create_all`` only creates *missing tables* — it never adds
columns to existing tables. This module closes that gap by emitting
``ALTER TABLE ... ADD COLUMN`` for any column the current models declare but
the live table lacks. It is strictly additive (no DROP, no data loss); new
columns are added NULLable so existing rows survive.

Called automatically on startup (see ``app.main`` lifespan) so a stale
``app.db`` is upgraded without a manual ``python migrate_db.py`` step.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from .db import engine, Base
from . import models  # noqa: F401  (registers all tables on Base.metadata)

logger = logging.getLogger("migrations")


def run() -> list[tuple[str, str, str]]:
    """Apply additive migrations. Returns the list of (table, column, type) added."""
    inspector = inspect(engine)
    added: list[tuple[str, str, str]] = []

    with engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if not inspector.has_table(table_name):
                # Table does not exist yet; create_all handles it at startup.
                continue
            existing = {c["name"] for c in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                ddl = (
                    f"ALTER TABLE {table_name} "
                    f"ADD COLUMN {col.name} {col_type} NULL"
                )
                conn.execute(text(ddl))
                added.append((table_name, col.name, col_type))

    if added:
        logger.info("Migration applied — columns added:")
        for t, c, ct in added:
            logger.info("  + %s.%s  (%s)", t, c, ct)
    else:
        logger.info("Schema already up to date — nothing to do.")

    # Additive migrations create nullable columns for compatibility. Backfill
    # legacy Campaign members so active-only queries continue to include them.
    if inspector.has_table("campaign_contacts"):
        with engine.begin() as conn:
            columns = {c["name"] for c in inspect(conn).get_columns("campaign_contacts")}
            if "membership_active" in columns:
                conn.execute(text(
                    "UPDATE campaign_contacts SET membership_active = 1 "
                    "WHERE membership_active IS NULL"
                ))

    # Repair legacy CRM rows created before terminal contact decisions cleared
    # their cached next action.  This is idempotent and only removes reply or
    # follow-up work from explicit terminal states; it does not alter messages,
    # contacts, or historical classification.
    if inspector.has_table("contacts"):
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE contacts SET next_action = 'none', next_follow_up_at = NULL "
                "WHERE (lifecycle_stage IN ('stopped', 'won') "
                "OR status IN ('unsubscribed', 'not_interested', 'bounced', 'archived')) "
                "AND (next_action IS NULL OR next_action != 'none' OR next_follow_up_at IS NOT NULL)"
            ))
            if result.rowcount:
                logger.info("Normalized terminal contact actions: %s row(s).", result.rowcount)

    # SQLite cannot relax the legacy NOT NULL campaign_id in place. Rebuild this
    # one table once so a global automation can exist without a dummy Campaign.
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            columns = conn.execute(text("PRAGMA table_info(automations)")).mappings().all()
            campaign_column = next((col for col in columns if col["name"] == "campaign_id"), None)
            if campaign_column and campaign_column["notnull"]:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("""
                    CREATE TABLE automations_global_ready (
                      id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
                      name VARCHAR(200) NOT NULL, prompt TEXT NOT NULL,
                      campaign_id INTEGER NULL REFERENCES campaigns(id),
                      scope VARCHAR(20) NOT NULL DEFAULT 'campaign', plan_json TEXT NOT NULL,
                      status VARCHAR(20) NOT NULL, tick_interval_minutes INTEGER NOT NULL,
                      next_run_at DATETIME NULL, last_run_at DATETIME NULL, last_status VARCHAR(20) NULL,
                      execution_mode VARCHAR(20) NOT NULL DEFAULT 'full_auto',
                      created_at DATETIME NULL, updated_at DATETIME NULL
                    )
                """))
                conn.execute(text("""
                    INSERT INTO automations_global_ready
                    (id, owner_id, name, prompt, campaign_id, scope, plan_json, status,
                     tick_interval_minutes, next_run_at, last_run_at, last_status,
                     execution_mode, created_at, updated_at)
                    SELECT id, owner_id, name, prompt, campaign_id, 'campaign', plan_json, status,
                           tick_interval_minutes, next_run_at, last_run_at, last_status,
                           COALESCE(execution_mode, 'full_auto'), created_at, updated_at
                    FROM automations
                """))
                conn.execute(text("DROP TABLE automations"))
                conn.execute(text("ALTER TABLE automations_global_ready RENAME TO automations"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_automations_owner_id ON automations(owner_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_automations_campaign_id ON automations(campaign_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_automations_scope ON automations(scope)"))
                conn.execute(text("PRAGMA foreign_keys=ON"))
                logger.info("Migrated automations for nullable global scope.")

    # Partial unique index: at most ONE inflight run per Automation.
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_automation_inflight"))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_automation_inflight "
            "ON automation_runs(automation_id) "
            "WHERE status IN ('queued', 'running', 'awaiting_confirmation', 'confirmed')"
        ))
    logger.info("Ensured index uq_automation_inflight (<=1 inflight run per automation).")
    if inspect(engine).has_table("gmail_sync_runs"):
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_gmail_sync_inflight"))
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_gmail_sync_inflight "
                "ON gmail_sync_runs(gmail_account_id) "
                "WHERE status IN ('queued', 'running', 'paused')"
            ))
        logger.info("Ensured index uq_gmail_sync_inflight (<=1 active sync per account).")
    if inspect(engine).has_table("campaign_generation_runs"):
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_campaign_generation_inflight"))
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_campaign_generation_inflight "
                "ON campaign_generation_runs(campaign_id) WHERE status = 'running'"
            ))
        logger.info("Ensured index uq_campaign_generation_inflight (<=1 active generation per campaign).")
    if inspect(engine).has_table("inbox_triage_runs"):
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_inbox_initial_triage_inflight"))
            conn.execute(text("DROP INDEX IF EXISTS uq_inbox_triage_inflight"))
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_inbox_triage_inflight "
                "ON inbox_triage_runs(owner_id) "
                "WHERE status IN ('queued', 'running', 'paused', 'recovery_pending')"
            ))
        logger.info("Ensured index uq_inbox_triage_inflight (<=1 active Inbox triage per Workspace).")
    return added
