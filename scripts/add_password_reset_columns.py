"""
Migration: add password_reset_token and password_reset_sent_at columns to users table.
Run once against production DB before deploying the new release.

Usage:
    python scripts/add_password_reset_columns.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.core.database import engine
from app.core.logger import logger


def column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return result.fetchone() is not None


def run():
    migrations = [
        ("password_reset_token",    "ALTER TABLE users ADD COLUMN password_reset_token VARCHAR(64)"),
        ("password_reset_sent_at",  "ALTER TABLE users ADD COLUMN password_reset_sent_at TIMESTAMP"),
    ]

    with engine.begin() as conn:
        for column, sql in migrations:
            if column_exists(conn, "users", column):
                logger.info(f"Column '{column}' already exists, skipping.")
            else:
                conn.execute(text(sql))
                logger.info(f"Column '{column}' added successfully.")

        # Create index on password_reset_token for fast lookups
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_users_password_reset_token "
            "ON users (password_reset_token)"
        ))
        logger.info("Index on password_reset_token ensured.")

    print("Migration completed successfully.")


if __name__ == "__main__":
    run()
