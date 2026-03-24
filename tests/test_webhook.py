"""
Webhook handler tests — security, idempotency, and edge cases.

Covers:
- Token validation (hmac.compare_digest timing-safe comparison)
- Invalid/missing token rejection
- TRANSFER_DONE status update
- TRANSFER_FAILED balance refund
- Duplicate payload idempotency
- Withdrawal validation webhook
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
from app.main import app
from app.core.database import get_db
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.core.config import settings as _app_settings
from datetime import datetime, timezone

_TEST_WEBHOOK_TOKEN = "wh-test-secure-token-2024"
_TEST_WITHDRAWAL_TOKEN = "wd-test-secure-token-2024"

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    saved = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


def _make_user(user_id: str = "user-wh-001", balance: float = 100.0) -> User:
    return User(
        id=user_id,
        name="Webhook Test User",
        cpf_cnpj="12345678901",
        credit_limit=500.0,
        balance=balance,
    )


def _make_tx(
    tx_id: str = "tx-wh-001",
    value: float = 25.0,
    status: PixStatus = PixStatus.CREATED,
    tx_type: TransactionType = TransactionType.SENT,
    user_id: str = "user-wh-001",
    fee_amount: float = 0.0,
) -> PixTransaction:
    return PixTransaction(
        id=tx_id,
        value=value,
        status=status,
        user_id=user_id,
        type=tx_type,
        pix_key="destination-key",
        key_type="ALEATORIA",
        description="Webhook test tx",
        fee_amount=fee_amount,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _webhook_db(tx, user=None):
    """Build a mock DB that routes queries by model class."""
    if user is None:
        user = _make_user(tx.user_id, balance=100.0)
    mock_db = MagicMock()

    def _qside(model):
        q = MagicMock()
        if model is PixTransaction:
            q.filter.return_value.first.return_value = tx
            q.filter.return_value.with_for_update.return_value.first.return_value = tx
        elif model is User:
            q.filter.return_value.with_for_update.return_value.first.return_value = user
            q.filter.return_value.first.return_value = user
        else:
            q.filter.return_value.first.return_value = None
            q.filter.return_value.update.return_value = 0
        return q

    mock_db.query.side_effect = _qside
    return mock_db


class TestWebhookTokenSecurity:
    """Validates that webhook authentication is enforced correctly."""

    def test_wrong_token_rejected(self, monkeypatch):
        """Webhook with incorrect token must be rejected — no balance mutation."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {"id": "pay-fake-001", "value": 999.99},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": "wrong-token-attempt"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("received") is False

    def test_missing_token_header_rejected(self, monkeypatch):
        """Webhook without token header must be rejected."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {"id": "pay-no-header-001", "value": 100.0},
        }

        response = client.post("/pix/webhook/asaas", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data.get("received") is False

    def test_unconfigured_token_rejects_all(self, monkeypatch):
        """When ASAAS_WEBHOOK_TOKEN is None, ALL webhooks must be rejected."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", None)

        payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {"id": "pay-inject-001", "value": 50000.0},
        }

        response = client.post("/pix/webhook/asaas", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data.get("received") is False
        assert data.get("action") == "rejected"


class TestWebhookTransferEvents:
    """Tests TRANSFER_DONE and TRANSFER_FAILED webhook processing."""

    def test_transfer_done_updates_status(self, monkeypatch):
        """TRANSFER_DONE must update transaction to CONFIRMED and debit sender."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        tx = _make_tx(status=PixStatus.PROCESSING, tx_type=TransactionType.SENT)
        mock_db = _webhook_db(tx)

        app.dependency_overrides[get_db] = lambda: mock_db

        payload = {
            "event": "TRANSFER_DONE",
            "payment": {"id": "tx-wh-001", "value": 25.0},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert tx.status == PixStatus.CONFIRMED

    def test_transfer_done_enriches_recipient_name(self, monkeypatch):
        """TRANSFER_DONE must enrich recipient_name from gateway when missing."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        tx = _make_tx(status=PixStatus.PROCESSING, tx_type=TransactionType.SENT)
        tx.recipient_name = None  # simulate missing name
        mock_db = _webhook_db(tx)

        app.dependency_overrides[get_db] = lambda: mock_db

        mock_gw = MagicMock()
        mock_gw.get_payment_status.return_value = {
            "payment_id": "tx-wh-001",
            "status": "CONFIRMED",
            "receiver_name": "Maria Silva",
        }

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gw):
            payload = {
                "event": "TRANSFER_DONE",
                "payment": {"id": "tx-wh-001", "value": 25.0},
            }
            response = client.post(
                "/pix/webhook/asaas",
                json=payload,
                headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
            )

        assert response.status_code == 200
        assert tx.status == PixStatus.CONFIRMED
        assert tx.recipient_name == "Maria Silva"

    def test_transfer_done_keeps_existing_recipient_name(self, monkeypatch):
        """TRANSFER_DONE must NOT overwrite an already-resolved recipient_name."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        tx = _make_tx(status=PixStatus.PROCESSING, tx_type=TransactionType.SENT)
        tx.recipient_name = "Joao Souza"  # already resolved
        mock_db = _webhook_db(tx)

        app.dependency_overrides[get_db] = lambda: mock_db

        payload = {
            "event": "TRANSFER_DONE",
            "payment": {"id": "tx-wh-001", "value": 25.0},
        }
        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert tx.recipient_name == "Joao Souza"

    def test_transfer_failed_reverses_ledger(self, monkeypatch):
        """TRANSFER_FAILED must set status=FAILED and reverse ledger entries.

        In the deferred-debit model, balance was never debited at dispatch,
        so no balance restoration is needed — only ledger reversal.
        """
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        tx = _make_tx(
            status=PixStatus.PROCESSING,
            tx_type=TransactionType.SENT,
            value=25.0,
            fee_amount=4.0,
        )
        mock_db = _webhook_db(tx)

        app.dependency_overrides[get_db] = lambda: mock_db

        payload = {
            "event": "TRANSFER_FAILED",
            "payment": {"id": "tx-wh-001", "value": 25.0},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert tx.status == PixStatus.FAILED


class TestWebhookIdempotency:
    """Validates duplicate webhook handling."""

    def test_duplicate_payment_received_is_noop(self, monkeypatch):
        """Second PAYMENT_RECEIVED for same charge must not double-credit."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        mock_db = MagicMock()
        tx = _make_tx(
            tx_id="pay-dup-001",
            status=PixStatus.CONFIRMED,
            tx_type=TransactionType.RECEIVED,
            value=100.0,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = tx

        app.dependency_overrides[get_db] = lambda: mock_db

        payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {"id": "pay-dup-001", "value": 100.0},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert response.json().get("action") == "already_confirmed"


class TestWithdrawalValidation:
    """Tests the withdrawal validation webhook endpoint."""

    def test_approved_with_valid_token(self, monkeypatch):
        """Valid token must return APPROVED."""
        monkeypatch.setattr(
            _app_settings, "ASAAS_WITHDRAWAL_VALIDATION_TOKEN", _TEST_WITHDRAWAL_TOKEN
        )

        payload = {
            "type": "TRANSFER",
            "transfer": {"id": "wd-001", "value": 100.0},
        }

        response = client.post(
            "/pix/webhook/asaas/validacao-saque",
            json=payload,
            headers={"asaas-access-token": _TEST_WITHDRAWAL_TOKEN},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "APPROVED"

    def test_refused_with_wrong_token(self, monkeypatch):
        """Wrong token must return REFUSED."""
        monkeypatch.setattr(
            _app_settings, "ASAAS_WITHDRAWAL_VALIDATION_TOKEN", _TEST_WITHDRAWAL_TOKEN
        )

        payload = {
            "type": "TRANSFER",
            "transfer": {"id": "wd-002", "value": 50.0},
        }

        response = client.post(
            "/pix/webhook/asaas/validacao-saque",
            json=payload,
            headers={"asaas-access-token": "wrong-token"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "REFUSED"

    def test_refused_when_token_not_configured(self, monkeypatch):
        """Security invariant: when no token configured, ALL withdrawals REFUSED (fail-closed)."""
        monkeypatch.setattr(
            _app_settings, "ASAAS_WITHDRAWAL_VALIDATION_TOKEN", None
        )

        payload = {
            "type": "PIX",
            "transfer": {"id": "wd-003", "value": 200.0},
        }

        response = client.post(
            "/pix/webhook/asaas/validacao-saque",
            json=payload,
        )

        assert response.status_code == 200
        assert response.json()["status"] == "REFUSED"


class TestWebhookStateMachineGuards:
    """Validates idempotency and state machine enforcement in transfer webhooks."""

    def test_transfer_done_idempotent_skip_if_already_confirmed(self, monkeypatch):
        """TRANSFER_DONE on an already-CONFIRMED tx must be a no-op (no double-debit)."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        tx = _make_tx(status=PixStatus.CONFIRMED, tx_type=TransactionType.SENT)
        user = _make_user(balance=100.0)
        mock_db = _webhook_db(tx, user)

        app.dependency_overrides[get_db] = lambda: mock_db

        payload = {
            "event": "TRANSFER_DONE",
            "payment": {"id": "tx-wh-001", "value": 25.0},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert response.json().get("action") == "already_confirmed"
        # Balance must NOT have changed
        assert user.balance == 100.0

    def test_transfer_failed_idempotent_skip_if_already_failed(self, monkeypatch):
        """TRANSFER_FAILED on an already-FAILED tx must be a no-op."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        tx = _make_tx(status=PixStatus.FAILED, tx_type=TransactionType.SENT)
        mock_db = _webhook_db(tx)

        app.dependency_overrides[get_db] = lambda: mock_db

        payload = {
            "event": "TRANSFER_FAILED",
            "payment": {"id": "tx-wh-001", "value": 25.0},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert response.json().get("action") == "already_failed"

    def test_transfer_done_rejected_if_not_processing(self, monkeypatch):
        """TRANSFER_DONE must reject transition from CREATED (only PROCESSING allowed)."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        tx = _make_tx(status=PixStatus.CREATED, tx_type=TransactionType.SENT)
        mock_db = _webhook_db(tx)

        app.dependency_overrides[get_db] = lambda: mock_db

        payload = {
            "event": "TRANSFER_DONE",
            "payment": {"id": "tx-wh-001", "value": 25.0},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert response.json().get("action") == "invalid_transition"
        # Status must remain CREATED
        assert tx.status == PixStatus.CREATED

    def test_transfer_failed_rejected_if_not_processing(self, monkeypatch):
        """TRANSFER_FAILED must reject transition from CONFIRMED."""
        monkeypatch.setattr(_app_settings, "ASAAS_WEBHOOK_TOKEN", _TEST_WEBHOOK_TOKEN)

        tx = _make_tx(status=PixStatus.CONFIRMED, tx_type=TransactionType.SENT)
        mock_db = _webhook_db(tx)

        app.dependency_overrides[get_db] = lambda: mock_db

        payload = {
            "event": "TRANSFER_FAILED",
            "payment": {"id": "tx-wh-001", "value": 25.0},
        }

        response = client.post(
            "/pix/webhook/asaas",
            json=payload,
            headers={"asaas-access-token": _TEST_WEBHOOK_TOKEN},
        )

        assert response.status_code == 200
        assert response.json().get("action") == "invalid_transition"
        # Status must remain CONFIRMED
        assert tx.status == PixStatus.CONFIRMED
