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

app.dependency_overrides  # accessed at module level; override is managed by fixture below

client = TestClient(app)

@pytest.fixture(scope="module", autouse=True)
def _setup_pix_v2_module() -> Generator[None, None, None]:
    """Installs and tears down the SQLite DB override for this module only."""
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override_get_db
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)

@pytest.fixture(scope="module")
def sender_token() -> str:
    # Create Sender User
    db = TestingSessionLocal()
    sender = User(
        name="Sender User",
        email="sender@test.com",
        cpf_cnpj="11111111111",
        hashed_password=get_password_hash("password123"),
        credit_limit=1000000000.0,  # High limit for testing
        email_verified=True,
    )
    db.add(sender)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "11111111111", "password": "password123"})
    assert response.status_code == 200
    return response.json()["access_token"]

@pytest.fixture(scope="module")
def receiver_token() -> str:
    # Create Receiver User
    db = TestingSessionLocal()
    receiver = User(
        name="Receiver User",
        email="receiver@test.com",
        cpf_cnpj="22222222222",
        hashed_password=get_password_hash("password123"),
        credit_limit=10000.0,
        email_verified=True,
    )
    db.add(receiver)
    db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "22222222222", "password": "password123"})
    assert response.status_code == 200
    return response.json()["access_token"]

def test_high_value_pix(sender_token: str, receiver_token: str) -> None:
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
    # Also update user.balance directly so service layer finds correct balance
    sender.balance += 2000000000.0
    db.add(sender)
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

    # 2. Receiver confirms the charge directly (simulates payment)
    confirm_payload = {"charge_id": charge_id}
    response = client.post("/pix/receber/confirmar", json=confirm_payload, cookies=receiver_cookies)
    assert response.status_code == 200, f"Confirm failed: {response.json()}"
    tx_data = response.json()

    assert tx_data["status"] == "CONFIRMADO"

    # 3. Verify Receiver got the money by checking statement
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

def test_self_deposit_simulation() -> None:
    """Test the Self-Deposit Simulation (Faucet) via Cobrar + Confirmar."""
    # Create a user with 0 balance (idempotent guard)
    db = TestingSessionLocal()
    test_cpf = "33333333333"
    existing = db.query(User).filter(User.cpf_cnpj == test_cpf).first()
    if not existing:
        depositor = User(
            name="Depositor User",
            email="depositor@test.com",
            cpf_cnpj=test_cpf,
            hashed_password=get_password_hash("password123"),
            credit_limit=0.0,
            email_verified=True,
        )
        db.add(depositor)
        db.commit()
    db.close()

    # Login
    response = client.post("/auth/login", json={"cpf_cnpj": "33333333333", "password": "password123"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    cookies = {"access_token": f"Bearer {token}"}

    # 1. Generate Charge
    charge_payload = {
        "value": 1000000000.0,
        "description": "My First Billion"
    }
    response = client.post("/pix/cobrar", json=charge_payload, cookies=cookies)
    assert response.status_code == 200
    charge_data = response.json()
    charge_id = charge_data["charge_id"]

    # 2. Confirm the charge directly (primary deposit flow)
    confirm_payload = {"charge_id": charge_id}
    response = client.post("/pix/receber/confirmar", json=confirm_payload, cookies=cookies)
    assert response.status_code == 200, f"Deposit failed: {response.json()}"
    tx_data = response.json()

    # Verify it returned the confirmed charge
    assert tx_data["status"] == "CONFIRMADO"
    assert tx_data["value"] == 1000000000.0

    # 3. Verify Balance
    response = client.get("/pix/extrato", cookies=cookies)
    assert response.status_code == 200
    statement = response.json()

    # Inbound from external bank: R$2 rede + R$1 manutencao = R$3.00 fee
    assert statement["balance"] == pytest.approx(999999997.0, abs=0.01)


def test_confirm_receipt_flow() -> None:
    """Test the 'Simular Pagamento' flow (Method A)."""
    # Create user with unique CPF suffix to avoid collision in shared module DB
    db = TestingSessionLocal()
    test_cpf = "44444444444"
    existing = db.query(User).filter(User.cpf_cnpj == test_cpf).first()
    if not existing:
        user = User(
            name="Receipt User",
            email="receipt@test.com",
            cpf_cnpj=test_cpf,
            hashed_password=get_password_hash("password123"),
            credit_limit=0.0,
            email_verified=True,
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
    assert statement["balance"] == pytest.approx(97.0, abs=0.01)  # R$100 - R$3 inbound fee

def test_high_value_receipt_flow() -> None:
    """Test receiving a very high value PIX (1 Billion) via Charge flow."""
    # Create user with idempotent guard to avoid collision in shared module DB
    db = TestingSessionLocal()
    test_cpf = "55555555555"
    existing = db.query(User).filter(User.cpf_cnpj == test_cpf).first()
    if not existing:
        user = User(
            name="High Value Receiver",
            email="rich@test.com",
            cpf_cnpj=test_cpf,
            hashed_password=get_password_hash("password123"),
            credit_limit=0.0,
            email_verified=True,
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
    assert statement["balance"] == pytest.approx(999999997.0, abs=0.01)  # R$1B - R$3 inbound fee


def test_self_transfer_blocked() -> None:
    """
    A user attempting to send PIX to their own CPF key must receive a 400
    with a clear error.  The internal-transfer router detects sender == recipient
    and raises ValueError before any balance change or transaction creation.
    """
    from app.pix.models import PixTransaction, PixStatus, TransactionType

    self_cpf = "66666666666"
    db = TestingSessionLocal()
    existing = db.query(User).filter(User.cpf_cnpj == self_cpf).first()
    if not existing:
        user = User(
            name="Self Transfer User",
            email="selftransfer@test.com",
            cpf_cnpj=self_cpf,
            hashed_password=get_password_hash("password123"),
            balance=100.0,
            credit_limit=0.0,
            email_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    db.close()

    login = client.post(
        "/auth/login",
        json={"cpf_cnpj": self_cpf, "password": "password123"},
    )
    assert login.status_code == 200, f"Login failed: {login.json()}"
    token = login.json()["access_token"]
    cookies = {"access_token": f"Bearer {token}"}

    # Attempt to transfer R$50 to own CPF key
    resp = client.post(
        "/pix/transacoes",
        json={
            "value": 50.0,
            "pix_key": self_cpf,
            "key_type": "CPF",
            "description": "Self transfer attempt",
        },
        headers={"X-Idempotency-Key": f"self-{self_cpf}"},
        cookies=cookies,
    )

    assert resp.status_code == 400, (
        f"Expected 400 for self-transfer, got {resp.status_code}: {resp.json()}"
    )
    detail = resp.json().get("detail", "")
    assert "propria conta" in detail.lower() or "proprio" in detail.lower(), (
        f"Expected self-transfer error message, got: {detail}"
    )


def test_active_account_with_balance_only_can_transfer() -> None:
    """
    A user with positive balance but no CONFIRMED RECEIVED PixTransaction
    (e.g., funds credited via admin panel) must be able to transfer to another
    internal user.  The active-account policy allows balance > 0 as an
    activation signal alongside has_deposit.
    """
    from app.pix.models import PixTransaction, PixStatus, TransactionType

    balance_only_cpf = "77777777777"
    recipient_cpf = "88888888888"

    db = TestingSessionLocal()
    for cpf, name, email, bal in [
        (balance_only_cpf, "Balance Only User", "balanceonly@test.com", 50.0),
        (recipient_cpf,    "Recipient Only",    "recipientonly@test.com", 0.0),
    ]:
        if not db.query(User).filter(User.cpf_cnpj == cpf).first():
            u = User(
                name=name,
                email=email,
                cpf_cnpj=cpf,
                hashed_password=get_password_hash("password123"),
                balance=bal,
                credit_limit=0.0,
                email_verified=True,
            )
            db.add(u)
    db.commit()
    db.close()

    login = client.post(
        "/auth/login",
        json={"cpf_cnpj": balance_only_cpf, "password": "password123"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    cookies = {"access_token": f"Bearer {token}"}

    # Transfer R$20 to recipient — sender has balance but NO CONFIRMED RECEIVED tx
    resp = client.post(
        "/pix/transacoes",
        json={
            "value": 20.0,
            "pix_key": recipient_cpf,
            "key_type": "CPF",
            "description": "Balance-only activation test",
        },
        headers={"X-Idempotency-Key": f"balonly-{balance_only_cpf}"},
        cookies=cookies,
    )

    assert resp.status_code == 201, (
        f"Expected 201 for user with positive balance (no RECEIVED tx), "
        f"got {resp.status_code}: {resp.json()}"
    )
    data = resp.json()
    assert data["value"] == 20.0
