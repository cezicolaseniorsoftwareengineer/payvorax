import os
import sys
from fastapi.testclient import TestClient
import logging

# FORCE LOCAL SQLITE
os.environ["DATABASE_URL"] = "sqlite:///./fintech.db"
os.environ["BIO_CODE_TECH_PAY_ALLOWED_START"] = "1"

# Add project root to sys.path
sys.path.append(os.getcwd())

from app.main import app
from app.auth.models import User
from app.auth.service import get_password_hash
from app.core.database import SessionLocal

# Setup logging to see errors
logging.basicConfig(level=logging.DEBUG)

def test_login_crash():
    print("--- TESTING LOGIN ---")

    # Ensure user exists
    cpf = "61425124000103"
    password = "password123"

    db = SessionLocal()
    user = db.query(User).filter(User.cpf_cnpj == cpf).first()
    if not user:
        print("Creating test user...")
        user = User(
             name="Test User",
             email="test@example.com",
             cpf_cnpj=cpf,
             hashed_password=get_password_hash(password)
        )
        db.add(user)
        db.commit()
    else:
        print("Updating test user password...")
        user.hashed_password = get_password_hash(password)
        db.commit()
    db.close()

    client = TestClient(app)

    try:
        response = client.post("/auth/login", json={"cpf_cnpj": cpf, "password": password})
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"CRASH: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_login_crash()
