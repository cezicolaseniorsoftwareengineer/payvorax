"""
Migration: add pix_random_key and pix_email_key columns to users table.
Backfills pix_random_key for every existing user that does not yet have one.

Run once:
    python scripts/migrate_pix_keys.py

Safe to re-run — already-migrated users are skipped.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from app.core.config import settings
from app.core.logger import logger


def run_migration() -> None:
    engine = create_engine(settings.DATABASE_URL)

    with engine.begin() as conn:
        # 1. Add columns if they do not exist yet.
        result_cols = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name IN ('pix_random_key', 'pix_email_key')"
            )
        )
        existing = {row[0] for row in result_cols}

        if "pix_random_key" not in existing:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN pix_random_key VARCHAR(36) UNIQUE"
            ))
            logger.info("Column pix_random_key added to users.")
        else:
            logger.info("Column pix_random_key already exists — skipped.")

        if "pix_email_key" not in existing:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN pix_email_key VARCHAR(100) UNIQUE"
            ))
            logger.info("Column pix_email_key added to users.")
        else:
            logger.info("Column pix_email_key already exists — skipped.")

        # 2. Backfill pix_random_key for users that do not have one yet.
        rows = conn.execute(
            text("SELECT id FROM users WHERE pix_random_key IS NULL")
        ).fetchall()

        updated = 0
        for row in rows:
            user_id = row[0]
            new_key = str(uuid.uuid4())
            conn.execute(
                text("UPDATE users SET pix_random_key = :key WHERE id = :id"),
                {"key": new_key, "id": user_id},
            )
            updated += 1

        logger.info(f"Backfilled pix_random_key for {updated} user(s).")

    print(f"Migration complete. {updated} user(s) received a new pix_random_key.")


if __name__ == "__main__":
    run_migration()
