
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from app.main import app
from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.core.config import settings as _app_settings
from datetime import datetime, timezone

_TEST_WEBHOOK_TOKEN = "wh-test-secure-token-2024"

client = TestClient(app)

# Mock User
mock_user = User(id="user-123", name="Test User", cpf_cnpj="12345678901", credit_limit=1000.0, balance=0.0)


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    """Restores app.dependency_overrides to the state before each test in this module.
    Uses save/restore instead of full clear to avoid destroying overrides from other modules."""
    saved = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


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
    # 3. Matrix account lookup inside credit_fee (returns None -> warning, no credit)
    # 4. build_pix_response -> Get Owner (User)
    # 5. build_pix_response -> Get Sender Transaction (PixTransaction) - None
    mock_db.query.return_value.filter.return_value.first.side_effect = [mock_tx, mock_user, None, mock_user, None]

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


def test_process_pix_receipt_already_paid():
    # Mock DB
    mock_db = MagicMock()

    # Mock existing transaction ALREADY CONFIRMED
    mock_tx = PixTransaction(
        id="charge-123",
        value=50.0,
        status=PixStatus.CONFIRMED,  # Already paid
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


# ---------------------------------------------------------------------------
# Webhook: fee deduction invariant
# ---------------------------------------------------------------------------

class TestAsaasWebhookFeeDeduction:
    """
    Verifies that the Asaas webhook charge-path deducts the platform receive fee
    from the user balance and credits the Matrix account.

    Critical regression guard: a previous version of the webhook credited pix.value
    (gross) directly to the user without computing net_credit, causing permanent
    over-credits that the audit equation could not detect.
    """

    def _make_charge_tx(self, value: float, status=PixStatus.CREATED):
        return PixTransaction(
            id="pay-abc-001",
            value=value,
            status=status,
            user_id="user-pf-001",
            type=TransactionType.RECEIVED,
            pix_key="RANDOM-KEY",
            key_type="ALEATORIA",
            description="Charge for webhook test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def _make_pf_user(self, balance: float = 0.0):
        return User(
            id="user-pf-001",
            name="PF Test User",
            cpf_cnpj="12345678901",  # CPF — 11 raw digits — PF
            credit_limit=500.0,
            balance=balance,
        )

    def _make_matrix_user(self, balance: float = 0.0):
        return User(
            id="matrix-001",
            name="Matrix Account",
            email="matrix@biocodetech.com",
            cpf_cnpj="00000000000",
            balance=balance,
            credit_limit=0.0,
        )

    def test_webhook_pf_charge_deducts_receive_fee(self, monkeypatch):
        """
        Inbound deposits from external banks carry a fee of R$3.00 (R$2 rede + R$1 manutencao).
        Deposit of R$9.25 via webhook must net R$6.25 to the user.
        Also verifies that the request is authenticated via the Asaas webhook token.
        """
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        gross = 9.25
        expected_fee = 3.00
        expected_net = round(gross - expected_fee, 2)

        mock_db = MagicMock()
        charge_tx = self._make_charge_tx(gross)
        pf_user = self._make_pf_user(balance=0.0)
        matrix_user = self._make_matrix_user(balance=0.0)

        # query(PixTransaction).filter().first -> charge_tx
        # query(User).filter(user_id).first -> pf_user
        # query(User).filter(matrix email).first -> matrix_user  [inside credit_fee]
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            charge_tx, pf_user, matrix_user,
        ]

        app.dependency_overrides[get_db] = lambda: mock_db

        webhook_payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {
                "id": "pay-abc-001",
                "value": gross,
                "customerName": "External Payer",
            },
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=webhook_payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("action") == "confirmed"

        # Balance must be net, never gross
        assert abs(pf_user.balance - expected_net) < 0.01, (
            f"PF user balance should be R${expected_net:.2f} (gross R${gross:.2f} - fee R${expected_fee:.2f}), "
            f"got R${pf_user.balance:.2f}."
        )
        # Matrix receives the fee amount
        assert abs(matrix_user.balance - expected_fee) < 0.01, (
            f"Matrix balance should be R${expected_fee:.2f}, got R${matrix_user.balance:.2f}."
        )

        mock_db.commit.assert_called()

    def test_webhook_already_confirmed_ignores_duplicate(self, monkeypatch):
        """Idempotency: second webhook for an already-confirmed charge must be a no-op."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        mock_db = MagicMock()
        charge_tx = self._make_charge_tx(9.25, status=PixStatus.CONFIRMED)

        mock_db.query.return_value.filter.return_value.first.return_value = charge_tx

        app.dependency_overrides[get_db] = lambda: mock_db

        webhook_payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {"id": "pay-abc-001", "value": 9.25},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=webhook_payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert response.json().get("action") == "already_confirmed"
        # Balance must never be mutated on duplicate
        assert charge_tx.status == PixStatus.CONFIRMED

    def test_webhook_pj_charge_deducts_receive_fee(self, monkeypatch):
        """
        Inbound deposits from external banks carry a fee of R$3.00 (R$2 rede + R$1 manutencao).
        Deposit of R$500.00 via webhook must net R$497.00 to the PJ user.
        Also verifies that the request is authenticated via the Asaas webhook token.
        """
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        gross = 500.00
        expected_fee = 3.00
        expected_net = round(gross - expected_fee, 2)

        mock_db = MagicMock()
        pj_tx = PixTransaction(
            id="pay-pj-001",
            value=gross,
            status=PixStatus.CREATED,
            user_id="user-pj-001",
            type=TransactionType.RECEIVED,
            pix_key="PJ-KEY",
            key_type="ALEATORIA",
            description="PJ deposit",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        pj_user = User(
            id="user-pj-001",
            name="PJ Company",
            cpf_cnpj="12345678000195",  # CNPJ — 14 raw digits — PJ
            credit_limit=10000.0,
            balance=0.0,
        )
        matrix_user = self._make_matrix_user(balance=0.0)

        mock_db.query.return_value.filter.return_value.first.side_effect = [
            pj_tx, pj_user, matrix_user,
        ]

        app.dependency_overrides[get_db] = lambda: mock_db

        webhook_payload = {
            "event": "PAYMENT_CONFIRMED",
            "payment": {"id": "pay-pj-001", "value": gross},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=webhook_payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert response.json().get("action") == "confirmed"

        assert abs(pj_user.balance - expected_net) < 0.01, (
            f"PJ user balance should be R${expected_net:.2f}, got R${pj_user.balance:.2f}"
        )
        assert abs(matrix_user.balance - expected_fee) < 0.01, (
            f"Matrix balance should be R${expected_fee:.2f}, got R${matrix_user.balance:.2f}"
        )

    def test_webhook_rejected_when_token_not_configured(self, monkeypatch):
        """Security invariant: when ASAAS_WEBHOOK_TOKEN is None, ALL webhooks must be rejected.
        This prevents unauthenticated callers from injecting fake PAYMENT_RECEIVED events
        to credit balances without a real Asaas payment.
        """
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", None)

        payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {"id": "pay-fake-inject", "value": 9999.99},
        }

        response = client.post("/pix/webhook/asaas", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data.get("received") is False
        assert data.get("action") == "rejected"
        assert data.get("reason") == "webhook_token_not_configured"
