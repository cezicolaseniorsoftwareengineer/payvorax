import sys
import os
import argparse

# FORCE LOCAL SQLITE
os.environ["DATABASE_URL"] = "sqlite:///./fintech.db"

# Add project root to sys.path
sys.path.append(os.getcwd())

from app.core.database import SessionLocal
from app.auth.models import User
import app.cards.models # Register models
from app.auth.service import get_password_hash

def set_password(cpf_cnpj, new_password):
    try:
        db = SessionLocal()
        # Clean CPF
        cpf_clean = "".join(filter(str.isdigit, cpf_cnpj))

        user = db.query(User).filter(User.cpf_cnpj == cpf_clean).first()

        if user:
            print(f"Updating password for user {user.name} ({user.cpf_cnpj})...")
            user.hashed_password = get_password_hash(new_password)
            db.commit()
            print(f"SUCCESS: Password updated!")
        else:
            print(f"User {cpf_clean} not found. Creating it...")
            hashed = get_password_hash(new_password)
            new_user = User(
                name="Admin User",
                email="admin@biocodetechpay.com",
                cpf_cnpj=cpf_clean,
                hashed_password=hashed
            )
            db.add(new_user)
            db.commit()
            print(f"SUCCESS: User created with the provided password!")

        db.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set user password manually")
    parser.add_argument("password", help="The new password to set")
    parser.add_argument("--cpf", default="61425124000103", help="CPF/CNPJ of the user")

    args = parser.parse_args()
    set_password(args.cpf, args.password)
