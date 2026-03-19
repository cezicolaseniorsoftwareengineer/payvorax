"""
Migration: add auto_renew column to user_subscriptions table.
Boolean, default True, NOT NULL.
Idempotent — safe to run multiple times.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import engine
from sqlalchemy import text

def migrate():
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'user_subscriptions' AND column_name = 'auto_renew'"
            )
        )
        if result.fetchone():
            print("[OK] Column 'auto_renew' already exists.")
            return

        conn.execute(
            text(
                "ALTER TABLE user_subscriptions "
                "ADD COLUMN auto_renew BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        conn.commit()
        print("[OK] Column 'auto_renew' added to user_subscriptions.")


if __name__ == "__main__":
    migrate()
