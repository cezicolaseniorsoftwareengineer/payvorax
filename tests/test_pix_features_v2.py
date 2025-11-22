import pytest
from typing import Generator, Any, Dict
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

def override_get_db() -> Generator[Any, None, None]:
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

@pytest.fixture(scope="module")
def test_db() -> Generator[None, None, None]:
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

@pytest.fixture(scope="module")
def sender_token(test_db: None) -> str:
    # Create Sender User
    db = TestingSessionLocal()
    sender = User(
        name="Sender User",
        email="sender@test.com",
        cpf_cnpj="11111111111",
        hashed_password=get_password_hash("password123"),
        credit_limit=1000000000.0  # High limit for testing
    )
    db.add(sender)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "11111111111", "password": "password123"})
    assert response.status_code == 200
    return response.json()["access_token"]

@pytest.fixture(scope="module")
def receiver_token(test_db: None) -> str:
    # Create Receiver User
    db = TestingSessionLocal()
    receiver = User(
        name="Receiver User",
        email="receiver@test.com",
        cpf_cnpj="22222222222",
        hashed_password=get_password_hash("password123"),
        credit_limit=10000.0
    )
    db.add(receiver)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "22222222222", "password": "password123"})
    assert response.status_code == 200
    return response.json()["access_token"]

def test_high_value_pix(sender_token: str) -> None:
    """Test sending a very high value PIX (1 Billion)."""
    cookies = {"access_token": f"Bearer {sender_token}"}
    headers = {
        "X-Idempotency-Key": "high-value-test-1"
    }
    payload: Dict[str, Any] = {
        "value": 1000000000.0,  # 1 Billion
        "key_type": "EMAIL",
        "pix_key": "receiver@test.com",
        "description": "High Value Test"
    }

    # First, we need to make sure the sender has balance.
    # In this system, balance is calculated from transactions.
    # We need to "deposit" money first or assume credit limit allows it?
    # The service checks: if data.value > current_balance: raise ValueError("Insufficient balance")
    # So we need to simulate a deposit first.

    # Simulate Deposit (Incoming PIX)
    # To deposit, we use type=RECEIVED, but the API endpoint /transacoes creates SENT by default unless logic changes.
    # Wait, the API /transacoes creates SENT.
    # How to deposit?
    # The system seems to lack a direct "Deposit" endpoint for users, usually deposits come from external webhooks.
    # However, for testing, we can manually insert a deposit into the DB or use the "Cobrar" feature if it allows self-payment? No.

    # Let's manually insert a deposit transaction for the sender in the DB
    db = TestingSessionLocal()
    from app.pix.models import PixTransaction, PixStatus, TransactionType
    from app.auth.models import User

    sender = db.query(User).filter(User.email == "sender@test.com").first()

    if not sender:
        pytest.fail("Sender user not found in test database")

    deposit = PixTransaction(
        id="deposit-1",
        value=2000000000.0,
        pix_key="11111111111",
        key_type="CPF",
        type=TransactionType.RECEIVED,
        status=PixStatus.CONFIRMED,
        idempotency_key="deposit-1",
        description="Initial Deposit",
        user_id=sender.id
    )
    db.add(deposit)
    db.commit()
    db.close()

    # Now try the transfer
    response = client.post("/pix/transacoes", json=payload, headers=headers, cookies=cookies)
    assert response.status_code == 201
    data = response.json()
    assert data["value"] == 1000000000.0
    assert data["status"] == "CONFIRMADO"

def test_copia_e_cola_flow(sender_token: str, receiver_token: str) -> None:
    """Test the Charge -> Copy Paste -> Pay flow."""

    # 1. Receiver generates a charge
    receiver_cookies = {"access_token": f"Bearer {receiver_token}"}
    charge_payload: Dict[str, Any] = {
        "value": 500.0,
        "description": "Payment for Services"
    }

    response = client.post("/pix/cobrar", json=charge_payload, cookies=receiver_cookies)
    assert response.status_code == 200
    charge_data = response.json()

    charge_id = charge_data["charge_id"]
    copy_paste_code = charge_data["copy_and_paste"]

    assert "000201" in copy_paste_code
    assert charge_id in copy_paste_code

    # 2. Sender pays using the Copy Paste code
    sender_cookies = {"access_token": f"Bearer {sender_token}"}
    sender_headers = {
        "X-Idempotency-Key": f"pay-charge-{charge_id}"
    }

    # The sender uses the copy_paste_code as the pix_key
    pay_payload: Dict[str, Any] = {
        "value": 500.0,
        "key_type": "ALEATORIA",
        "pix_key": copy_paste_code,
        "description": "Paying the charge"
    }

    response = client.post("/pix/transacoes", json=pay_payload, headers=sender_headers, cookies=sender_cookies)
    assert response.status_code == 201
    tx_data = response.json()

    assert tx_data["status"] == "CONFIRMADO"

    # 3. Verify Receiver got the money
    # We can check the statement of the receiver
    response = client.get("/pix/extrato", cookies=receiver_cookies)
    assert response.status_code == 200
    statement = response.json()

    # Find the transaction
    found = False
    for tx in statement["transactions"]:
        if tx["id"] == charge_id and tx["status"] == "CONFIRMADO" and tx["type"] == "RECEBIDO":
            found = True
            break

    assert found, "Receiver did not receive the confirmed charge transaction"

def test_self_deposit_simulation(test_db: None) -> None:
    """Test the Self-Deposit Simulation (Faucet) via Copia e Cola."""
    # Create a user with 0 balance
    db = TestingSessionLocal()
    depositor = User(
        name="Depositor User",
        email="depositor@test.com",
        cpf_cnpj="33333333333",
        hashed_password=get_password_hash("password123"),
        credit_limit=0.0
    )
    db.add(depositor)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "33333333333", "password": "password123"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    cookies = {"access_token": f"Bearer {token}"}

    # 1. Generate Charge (1 Billion)
    charge_payload = {
        "value": 1000000000.0,
        "description": "My First Billion"
    }
    response = client.post("/pix/cobrar", json=charge_payload, cookies=cookies)
    assert response.status_code == 200
    charge_data = response.json()
    copy_paste_code = charge_data["copy_and_paste"]

    # 2. Pay the Charge (Self-Payment)
    headers = {
        "X-Idempotency-Key": "self-deposit-1"
    }
    pay_payload = {
        "value": 1000000000.0,
        "key_type": "ALEATORIA",
        "pix_key": copy_paste_code,
        "description": "Injecting Money"
    }

    response = client.post("/pix/transacoes", json=pay_payload, headers=headers, cookies=cookies)
    assert response.status_code == 201
    tx_data = response.json()

    # Verify it returned the confirmed charge
    assert tx_data["status"] == "CONFIRMADO"
    assert tx_data["value"] == 1000000000.0

    # 3. Verify Balance
    response = client.get("/pix/extrato", cookies=cookies)
    assert response.status_code == 200
    statement = response.json()

    # Balance should be 1 Billion (Received) - 0 (Sent skipped) = 1 Billion
    assert statement["balance"] == 1000000000.0

def test_confirm_receipt_flow(test_db: None) -> None:
    """Test the 'Simular Pagamento' flow (Method A)."""
    # Create user
    db = TestingSessionLocal()
    user = User(
        name="Receipt User",
        email="receipt@test.com",
        cpf_cnpj="44444444444",
        hashed_password=get_password_hash("password123"),
        credit_limit=0.0
    )
    db.add(user)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "44444444444", "password": "password123"})
    token = response.json()["access_token"]
    cookies = {"access_token": f"Bearer {token}"}

    # 1. Generate Charge
    charge_payload = {"value": 100.0, "description": "Test Receipt"}
    response = client.post("/pix/cobrar", json=charge_payload, cookies=cookies)
    assert response.status_code == 200
    charge_id = response.json()["charge_id"]

    # 2. Confirm Receipt (Simulate external payment)
    confirm_payload = {"charge_id": charge_id}
    response = client.post("/pix/receber/confirmar", json=confirm_payload, cookies=cookies)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "CONFIRMADO"
    assert data["value"] == 100.0

    # 3. Verify Balance
    response = client.get("/pix/extrato", cookies=cookies)
    statement = response.json()
    assert statement["balance"] == 100.0

def test_high_value_receipt_flow(test_db: None) -> None:
    """Test receiving a very high value PIX (1 Billion) via Charge flow."""
    # Create user
    db = TestingSessionLocal()
    user = User(
        name="High Value Receiver",
        email="rich@test.com",
        cpf_cnpj="55555555555",
        hashed_password=get_password_hash("password123"),
        credit_limit=0.0
    )
    db.add(user)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "55555555555", "password": "password123"})
    token = response.json()["access_token"]
    cookies = {"access_token": f"Bearer {token}"}

    # 1. Generate Charge (1 Billion)
    charge_payload = {"value": 1000000000.0, "description": "Billion Dollar Deal"}
    response = client.post("/pix/cobrar", json=charge_payload, cookies=cookies)
    assert response.status_code == 200
    charge_id = response.json()["charge_id"]

    # 2. Confirm Receipt
    confirm_payload = {"charge_id": charge_id}
    response = client.post("/pix/receber/confirmar", json=confirm_payload, cookies=cookies)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "CONFIRMADO"
    assert data["value"] == 1000000000.0

    # 3. Verify Balance
    response = client.get("/pix/extrato", cookies=cookies)
    statement = response.json()
    assert statement["balance"] == 1000000000.0
