
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from app.main import app
from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from datetime import datetime, timezone

client = TestClient(app)

# Mock User
mock_user = User(id="user-123", name="Test User", cpf_cnpj="12345678901", credit_limit=1000.0)

def test_generate_pix_charge():
    # Mock DB
    mock_db = MagicMock()

    # Override dependencies
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: mock_user

    payload: dict[str, object] = {
        "value": 50.0,
        "description": "Test Charge"
    }

    response = client.post("/pix/cobrar", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert "charge_id" in data
    assert data["value"] == 50.0
    assert "qr_code_url" in data

    # Verify DB interaction
    mock_db.add.assert_called()
    mock_db.commit.assert_called()

    # Clean up
    app.dependency_overrides = {}

def test_process_pix_receipt_success():
    # Mock DB
    mock_db = MagicMock()

    # Mock existing transaction
    mock_tx = PixTransaction(
        id="charge-123",
        value=50.0,
        status=PixStatus.CREATED,
        user_id="user-123",
        type=TransactionType.RECEIVED,
        pix_key="RANDOM-KEY",
        key_type="ALEATORIA",
        description="Test Charge",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )

    # Setup query return
    # 1. Get Charge (PixTransaction)
    # 2. Get Receiver (User)
    # 3. build_pix_response -> Get Owner (User)
    # 4. build_pix_response -> Get Sender Transaction (PixTransaction) - None
    mock_db.query.return_value.filter.return_value.first.side_effect = [mock_tx, mock_user, mock_user, None]

    # Override dependencies
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: mock_user

    payload = {"charge_id": "charge-123"}

    response = client.post("/pix/receber/confirmar", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == PixStatus.CONFIRMED

    # Verify status update
    assert mock_tx.status == PixStatus.CONFIRMED
    mock_db.commit.assert_called()

    # Clean up
    app.dependency_overrides = {}

def test_process_pix_receipt_already_paid():
    # Mock DB
    mock_db = MagicMock()

    # Mock existing transaction ALREADY CONFIRMED
    mock_tx = PixTransaction(
        id="charge-123",
        value=50.0,
        status=PixStatus.CONFIRMED, # Already paid
        user_id="user-123",
        type=TransactionType.RECEIVED,
        key_type="ALEATORIA",
        pix_key="RANDOM-KEY",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )

    # Setup query return
    mock_db.query.return_value.filter.return_value.first.return_value = mock_tx

    # Override dependencies
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: mock_user

    payload = {"charge_id": "charge-123"}

    response = client.post("/pix/receber/confirmar", json=payload)

    # Expect 409 Conflict
    assert response.status_code == 409
    assert "já foi paga" in response.json()["detail"]

    # Clean up
    app.dependency_overrides = {}

def test_process_pix_receipt_not_found():
    # Mock DB
    mock_db = MagicMock()

    # Setup query return None
    mock_db.query.return_value.filter.return_value.first.return_value = None

    # Override dependencies
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: mock_user

    payload = {"charge_id": "charge-999"}

    response = client.post("/pix/receber/confirmar", json=payload)

    assert response.status_code == 404

    # Clean up
    app.dependency_overrides = {}
