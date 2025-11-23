import sys
import os

# Add project root to python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.core.database import engine
from app.core.logger import logger

def optimize_indexes():
    """
    Creates composite indexes to optimize PIX balance calculation and statement listing.
    """
    logger.info("Starting database optimization...")

    with engine.connect() as conn:
        # 1. Index for Balance Calculation (SUM queries)
        # Covers: user_id, status, type, and includes value for index-only scan
        try:
            logger.info("Creating index: idx_pix_balance_calc")
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_pix_balance_calc
                ON transacoes_pix (user_id, status, tipo, valor);
            """))
            logger.info("Index idx_pix_balance_calc created/verified.")
        except Exception as e:
            logger.warning(f"Could not create idx_pix_balance_calc: {e}")

        # 2. Index for Statement Listing (Pagination)
        # Covers: user_id filtering and created_at sorting
        try:
            logger.info("Creating index: idx_pix_user_created")
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_pix_user_created
                ON transacoes_pix (user_id, criado_em DESC);
            """))
            logger.info("Index idx_pix_user_created created/verified.")
        except Exception as e:
            logger.warning(f"Could not create idx_pix_user_created: {e}")

        conn.commit()

    logger.info("Database optimization completed.")

if __name__ == "__main__":
    optimize_indexes()
