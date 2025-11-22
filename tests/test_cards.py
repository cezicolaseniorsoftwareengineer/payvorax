import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.database import Base, get_db
from app.auth.models import User
from app.core.security import get_password_hash

# Setup In-Memory Database for Testing
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
        email="card@test.com",
        cpf_cnpj="99999999999",
        hashed_password=get_password_hash("password123"),
        credit_limit=1000.0
    )
    db.add(user)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "99999999999", "password": "password123"})
    assert response.status_code == 200
    return response.json()["access_token"]

def test_create_virtual_card(auth_token):
    cookies = {"access_token": f"Bearer {auth_token}"}

    # Create Multiuso Card
    payload = {"type": "VIRTUAL_MULTUSE"}
    response = client.post("/cards/", json=payload, cookies=cookies)
    assert response.status_code == 201
    data = response.json()
    assert data["type"] == "VIRTUAL_MULTUSE"
    assert data["card_number"].startswith("4")
    assert data["expires_at"] is None

    # Create Temp Card
    payload = {"type": "VIRTUAL_TEMP"}
    response = client.post("/cards/", json=payload, cookies=cookies)
    assert response.status_code == 201
    data = response.json()
    assert data["type"] == "VIRTUAL_TEMP"
    assert data["expires_at"] is not None

def test_list_cards(auth_token):
    cookies = {"access_token": f"Bearer {auth_token}"}
    response = client.get("/cards/", cookies=cookies)
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2
