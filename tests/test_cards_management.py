
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.database import Base, get_db
from app.auth.models import User
from app.core.security import get_password_hash

# Setup In-Memory Database
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

@pytest.fixture(scope="module")
def test_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

@pytest.fixture(scope="module")
def auth_token(test_db):
    # Create User
    db = TestingSessionLocal()
    user = User(
        name="Card User",
        email="carduser@test.com",
        cpf_cnpj="99988877766",
        hashed_password=get_password_hash("password123"),
        credit_limit=5000.0
    )
    db.add(user)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "99988877766", "password": "password123"})
    return response.json()["access_token"]

def test_card_lifecycle(auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    cookies = {"access_token": f"Bearer {auth_token}"}

    # 1. Create Card
    response = client.post("/cards/", json={"type": "VIRTUAL_MULTUSE"}, cookies=cookies)
    assert response.status_code == 201
    card = response.json()
    card_id = card["id"]
    assert card["limit"] == 1000.0
    assert card["is_blocked"] == False

    # 2. Block Card
    response = client.post(f"/cards/{card_id}/block", cookies=cookies)
    assert response.status_code == 200
    assert response.json()["is_blocked"] == True

    # 3. Unblock Card
    response = client.post(f"/cards/{card_id}/block", cookies=cookies)
    assert response.status_code == 200
    assert response.json()["is_blocked"] == False

    # 4. Update Limit
    response = client.patch(f"/cards/{card_id}/limit", json={"limit": 2500.0}, cookies=cookies)
    assert response.status_code == 200
    assert response.json()["limit"] == 2500.0

    # 5. Delete Card
    response = client.delete(f"/cards/{card_id}", cookies=cookies)
    assert response.status_code == 204

    # 6. Verify Deletion
    response = client.get("/cards/", cookies=cookies)
    cards = response.json()
    assert len([c for c in cards if c["id"] == card_id]) == 0
