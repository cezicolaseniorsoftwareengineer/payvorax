"""
Tests for the static deposit verification endpoint (POST /pix/deposito/verificar).

Covers:
- Matching deposit found and credited
- No matching deposit returns zero
- Idempotent: already processed deposit is not double-credited
- Gateway unavailable returns 503
- User without CPF/CNPJ returns 400
- Masked CPF matching (Asaas format)
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType


client = TestClient(app)

_mock_user = User(
    id="user-dep-001",
    name="Depositante Teste",
    cpf_cnpj="12360268840",
    credit_limit=1000.0,
    balance=0.0,
    is_admin=False,
)


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    saved = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


def _make_mock_db():
    mock_db = MagicMock()
    # Default: no existing transaction found (for idempotency checks)
    mock_db.query.return_value.filter.return_value.first.return_value = None
    return mock_db


def _asaas_pix_credit(tx_id, value, sender_cpf, sender_name="Fulano", payment_id=None):
    """Builds a mock Asaas PIX transaction dict."""
    tx = {
        "id": tx_id,
        "value": value,
        "type": "CREDIT",
        "status": "DONE",
        "externalAccount": {
            "name": sender_name,
            "cpfCnpj": sender_cpf,
        },
    }
    if payment_id:
        tx["payment"] = payment_id
    return tx


class TestDepositVerification:

    def test_matching_deposit_credited(self):
        """Full CPF match: deposit is found and credited."""
        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        credits = [_asaas_pix_credit("pix-tx-001", 100.0, "12360268840", "Depositante Teste")]
        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = credits

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 200
        data = response.json()
        assert data["credited_count"] == 1
        assert data["credited_total"] > 0

    def test_no_matching_deposit(self):
        """Different CPF: no deposit matched."""
        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        credits = [_asaas_pix_credit("pix-tx-002", 50.0, "99988877766", "Outro Usuario")]
        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = credits

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 200
        data = response.json()
        assert data["credited_count"] == 0
        assert data["credited_total"] == 0

    def test_idempotent_already_processed(self):
        """Deposit already processed (idempotency_key exists): not double-credited."""
        mock_db = MagicMock()
        # First query (idempotency check) returns existing transaction
        existing_tx = MagicMock()
        existing_tx.id = "pix-tx-003"
        mock_db.query.return_value.filter.return_value.first.return_value = existing_tx

        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        credits = [_asaas_pix_credit("pix-tx-003", 200.0, "12360268840")]
        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = credits

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 200
        data = response.json()
        assert data["credited_count"] == 0

    def test_gateway_unavailable_returns_503(self):
        """Gateway returning None means service unavailable."""
        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        with patch("app.pix.router.get_payment_gateway", return_value=None):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 503

    def test_user_without_cpf_returns_400(self):
        """User without CPF/CNPJ cannot verify deposits."""
        mock_db = _make_mock_db()
        no_cpf_user = User(
            id="user-dep-002",
            name="Sem CPF",
            cpf_cnpj="",
            credit_limit=0.0,
            balance=0.0,
        )
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: no_cpf_user

        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = []

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 400

    def test_masked_cpf_match(self):
        """Asaas-masked CPF (6 middle digits) matches user."""
        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        # Asaas masks CPF as ***.602.688-** → after stripping non-digits: "602688"
        credits = [_asaas_pix_credit("pix-tx-004", 75.0, "***.602.688-**", "Depositante")]
        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = credits

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 200
        data = response.json()
        # User CPF 12360268840 → middle digits [3:9] = "602688" — matches masked
        assert data["credited_count"] == 1

    def test_empty_credits_returns_zero(self):
        """No PIX credits in the period: returns zero."""
        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = []

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 200
        data = response.json()
        assert data["credited_count"] == 0
        assert data["credited_total"] == 0
        assert "balance" in data

    def test_webhook_already_processed_skips(self):
        """If webhook already processed via payment_id, deposit verification skips it."""
        mock_db = MagicMock()
        # First call (idempotency_key check) returns None, second call (payment_id check) returns existing
        existing_tx = MagicMock()
        existing_tx.id = "pay_webhook_001"
        mock_db.query.return_value.filter.return_value.first.side_effect = [None, existing_tx]

        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        credits = [_asaas_pix_credit("pix-tx-005", 150.0, "12360268840", payment_id="pay_webhook_001")]
        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = credits

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 200
        data = response.json()
        assert data["credited_count"] == 0

    def test_zero_value_deposit_ignored(self):
        """Deposit with value <= 0 is ignored."""
        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        credits = [_asaas_pix_credit("pix-tx-006", 0, "12360268840")]
        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = credits

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 200
        assert response.json()["credited_count"] == 0

    def test_webhook_correlation_id_prevents_double_credit(self):
        """If webhook stored tx_id as correlation_id, verify skips (Check 3).

        Covers the edge case where tx.get('payment') is None (absent from
        Asaas /pix/transactions response) but the webhook already credited
        and stored the pix tx id as PixTransaction.correlation_id.
        """
        mock_db = MagicMock()
        # Check 1 (idempotency_key): not found
        # Check 2 (payment_id): None — block is skipped
        # Check 3 (correlation_id): found — triggers skip
        existing_corr = MagicMock()
        existing_corr.id = "pay_webhook_corr"
        # side_effect: first call (idemp_key) → None, second call (correlation_id) → found
        mock_db.query.return_value.filter.return_value.first.side_effect = [None, existing_corr]

        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: _mock_user

        # No payment_id in transaction (None) — simulates missing field from Asaas
        credits = [_asaas_pix_credit("pix-tx-007", 100.0, "12360268840")]
        mock_gateway = MagicMock()
        mock_gateway.list_pix_credits.return_value = credits

        with patch("app.pix.router.get_payment_gateway", return_value=mock_gateway):
            response = client.post("/pix/deposito/verificar")

        assert response.status_code == 200
        data = response.json()
        assert data["credited_count"] == 0
