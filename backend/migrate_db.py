"""CLI entrypoint for the additive SQLite migration.

The implementation lives in ``app.migrations`` so it can also be called from
the application lifespan on startup. Run from the backend directory:

    python migrate_db.py
"""
from __future__ import annotations

from app.migrations import run


def main() -> None:
    added = run()
    if added:
        print("Migration applied — columns added:")
        for t, c, ct in added:
            print(f"  + {t}.{c}  ({ct})")
    else:
        print("Schema already up to date — nothing to do.")
    print("Ensured index uq_automation_inflight (<=1 inflight run per automation).")


if __name__ == "__main__":
    main()
