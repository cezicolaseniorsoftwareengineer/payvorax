"""
Backfill shared deposit wallet ID for all existing users.

Runs against the production Neon database (DATABASE_URL from .env or environment).
Safe to re-run: uses WHERE clause to skip users already updated.

Usage:
  python scripts/backfill_shared_wallet.py
  python scripts/backfill_shared_wallet.py --dry-run
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from app.core.config import settings

SHARED_DEPOSIT_WALLET_ID = "48a5b50d-902e-4d5f-8b40-8a9eeb093456"


def run(dry_run: bool = False) -> None:
    engine = create_engine(settings.DATABASE_URL)

    with engine.connect() as conn:
        count_result = conn.execute(
            text(
                "SELECT COUNT(*) FROM users WHERE asaas_wallet_id IS DISTINCT FROM :wallet_id"
            ),
            {"wallet_id": SHARED_DEPOSIT_WALLET_ID},
        )
        pending = count_result.scalar()
        print(f"Users pending update: {pending}")

        if pending == 0:
            print("All users already have the shared deposit wallet. Nothing to do.")
            return

        if dry_run:
            print(f"[DRY RUN] Would UPDATE {pending} users -> asaas_wallet_id = '{SHARED_DEPOSIT_WALLET_ID}'")
            return

        result = conn.execute(
            text(
                "UPDATE users SET asaas_wallet_id = :wallet_id "
                "WHERE asaas_wallet_id IS DISTINCT FROM :wallet_id"
            ),
            {"wallet_id": SHARED_DEPOSIT_WALLET_ID},
        )
        conn.commit()
        print(f"Updated {result.rowcount} users -> asaas_wallet_id = '{SHARED_DEPOSIT_WALLET_ID}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill shared deposit wallet ID")
    parser.add_argument("--dry-run", action="store_true", help="Show pending count without writing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
