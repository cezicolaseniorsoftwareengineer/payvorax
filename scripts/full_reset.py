import sys
import os

# 1. FORCE LOCAL SQLITE
os.environ["DATABASE_URL"] = "sqlite:///./fintech.db"
os.environ["BIO_CODE_TECH_PAY_ALLOWED_START"] = "1" # Allow config to load if it checks this

# Add project root to sys.path
sys.path.append(os.getcwd())

from app.core.database import init_db, SessionLocal
from app.auth.models import User
from app.auth.service import get_password_hash

# 2. IMPORT ALL MODELS TO REGISTER WITH BASE
# This is crucial for create_all to see them
import app.auth.models
import app.pix.models
import app.cards.models
import app.antifraude.models
import app.boleto.models
import app.parcelamento.models

def full_reset():
    print("--- FULL DATABASE RESET & REPAIR ---")

    # Init DB (Creates tables for all imported models)
    try:
        init_db()
        print("[SUCCESS] Database schema initialized.")
    except Exception as e:
        print(f"[ERROR] Failed to init DB: {e}")
        return

    # Create Default User
    try:
        db = SessionLocal()
        cpf_clean = "61425124000103"

        # Check if already exists (it shouldn't if we just wiped it, but good practice)
        user = db.query(User).filter(User.cpf_cnpj == cpf_clean).first()

        new_password = "12345678"
        hashed = get_password_hash(new_password)

        if not user:
             new_user = User(
                name="Bio Code Tech Pay Utils",
                email="admin@biocodetechpay.com.br",
                cpf_cnpj=cpf_clean,
                hashed_password=hashed,
                credit_limit=50000.00
            )
             db.add(new_user)
             db.commit()
             print(f"[SUCCESS] Admin User Created: CPF={cpf_clean}, Pass={new_password}")
        else:
             print("[INFO] User already exists.")

        db.close()
    except Exception as e:
        print(f"[ERROR] Failed to seed user: {e}")

if __name__ == "__main__":
    full_reset()
