"""Idempotent migration: add 'trial_used' BOOLEAN column to user_subscriptions."""
import os, sys, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("migrate_trial_used")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text, inspect
from app.core.database import engine


def run():
    with engine.connect() as conn:
        inspector = inspect(conn)
        cols = [c["name"] for c in inspector.get_columns("user_subscriptions")]
        if "trial_used" in cols:
            log.info("[OK] Column 'trial_used' already exists. Nothing to do.")
            return
        conn.execute(text(
            "ALTER TABLE user_subscriptions ADD COLUMN trial_used BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.commit()
        log.info("[OK] Column 'trial_used' added to user_subscriptions.")


if __name__ == "__main__":
    run()
