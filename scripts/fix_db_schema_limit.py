import sys
import os

# Add the project root to the python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.core.database import engine
from app.core.logger import logger

def add_limit_column():
    logger.info("Attempting to add 'limit' column to 'credit_cards' table...")
    with engine.connect() as connection:
        try:
            # Using a transaction
            with connection.begin():
                # Postgres supports IF NOT EXISTS for ADD COLUMN
                connection.execute(text('ALTER TABLE credit_cards ADD COLUMN IF NOT EXISTS "limit" FLOAT DEFAULT 0.0;'))

            logger.info("Successfully added 'limit' column.")
        except Exception as e:
            logger.error(f"Error adding column: {e}")
            raise e

if __name__ == "__main__":
    add_limit_column()
