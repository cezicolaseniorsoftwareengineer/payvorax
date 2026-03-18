"""
Tests for QR Code payment flows:
  - QR Code camera (internal simulation charge)
  - Copia e Cola (EMV paste — internal and external/Asaas)
  - Chave aleatoria (random EVP code — external via Asaas mock)
  - Guards: zero balance, insufficient balance, double payment, idempotency
  - Self-deposit (payer == receiver)
  - Payload validation
  - Asaas error propagation
"""
import pytest
from typing import Generator, Any, Dict
from unittest.mock import patch, MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.database import Base, get_db
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.pix.schemas import PixKeyType
from app.core.security import get_password_hash

# ---------------------------------------------------------------------------
# In-memory SQLite DB — isolated from production Neon PostgreSQL
# ---------------------------------------------------------------------------
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


client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _setup_module() -> Generator[None, None, None]:
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override_get_db
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


# ---------------------------------------------------------------------------
# Fixtures: payer (com saldo) e receiver (sem saldo, gera cobranças)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def payer_token() -> str:
    """Payer user: starts with R$500 balance (injected via direct DB write)."""
    db = TestingSessionLocal()
    payer = User(
        name="Payer QR Test",
        email="payer.qr@test.com",
        cpf_cnpj="77700000001",
        hashed_password=get_password_hash("pass123"),
        balance=500.0,
        credit_limit=0.0,
        email_verified=True,
    )
    db.add(payer)
    db.commit()
    db.close()

    resp = client.post("/auth/login", json={"cpf_cnpj": "77700000001", "password": "pass123"})
    assert resp.status_code == 200, f"payer login failed: {resp.json()}"
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def receiver_token() -> str:
    """Receiver user: creates charges that the payer will pay."""
    db = TestingSessionLocal()
    receiver = User(
        name="Receiver QR Test",
        email="receiver.qr@test.com",
        cpf_cnpj="77700000002",
        hashed_password=get_password_hash("pass123"),
        balance=0.0,
        credit_limit=0.0,
        email_verified=True,
    )
    db.add(receiver)
    db.commit()
    db.close()

    resp = client.post("/auth/login", json={"cpf_cnpj": "77700000002", "password": "pass123"})
    assert resp.status_code == 200, f"receiver login failed: {resp.json()}"
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Helper: build a valid BR PIX EMV payload with optional field 54 (amount)
# ---------------------------------------------------------------------------

def _build_emv(charge_id: str, value: float | None = None) -> str:
    """
    Builds a BR PIX EMV string that embeds a charge UUID (Routing 1a detection).
    Optionally embeds field 54 (Transaction Amount) for the client-side value parser.
    Format: 00020126580014BR.GOV.BCB.PIX0136<uuid>52040000...6304
    """
    base = (
        f"00020126580014BR.GOV.BCB.PIX0136{charge_id}"
        "52040000530398658002BR5921BioCodeTechPay6008BRASILIA"
    )
    if value is not None:
        amount_str = f"{value:.2f}"
        tag = f"54{len(amount_str):02d}{amount_str}"
        base = base + tag
    base += "62070503***6304ABCD"
    return base


# ===========================================================================
# 1. QR CODE CAMERA — internal simulation charge
# ===========================================================================

class TestQrCodeCamera:
    """
    Simulates a phone camera scanning a QR Code that encodes an internal
    BioCodeTechPay simulation charge (contains UUID in EMV). Routing 1a path.
    """

    def test_camera_qr_pays_internal_charge_and_debits_balance(
        self, payer_token: str, receiver_token: str
    ) -> None:
        """
        Full flow:
          1. Receiver generates a charge (POST /pix/cobrar)
          2. Payer scans QR — payload sent to POST /pix/qrcode/pagar
          3. Assert: payer balance debited, receiver balance credited, status CONFIRMADO
        """
        receiver_cookies = {"access_token": f"Bearer {receiver_token}"}
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        # Step 1: receiver generates charge (R$50)
        charge_resp = client.post(
            "/pix/cobrar",
            json={"value": 50.0, "description": "Camera QR test charge"},
            cookies=receiver_cookies,
        )
        assert charge_resp.status_code == 200, f"cobrar failed: {charge_resp.json()}"
        charge_data = charge_resp.json()
        charge_id = charge_data["charge_id"]

        # Step 2: payer "scans" — sends EMV payload with embedded UUID
        emv_payload = _build_emv(charge_id, value=50.0)
        pay_resp = client.post(
            "/pix/qrcode/pagar",
            json={"payload": emv_payload, "description": "Camera scan payment"},
            headers={"X-Idempotency-Key": f"cam-{charge_id}"},
            cookies=payer_cookies,
        )
        assert pay_resp.status_code == 200, f"qrcode/pagar failed: {pay_resp.json()}"
        pay_data = pay_resp.json()

        # Step 3: validate result
        assert pay_data["status"] == "CONFIRMADO"
        assert pay_data["value"] == 50.0
        assert pay_data["type"] == "ENVIADO"

        # Check payer balance was debited
        payer_statement = client.get("/pix/extrato", cookies=payer_cookies).json()
        assert payer_statement["balance"] == 449.0, (
            f"Payer balance should be R$449 (500-50-1 taxa de manutencao), got {payer_statement['balance']}"
        )

        # Check receiver balance was credited
        receiver_statement = client.get("/pix/extrato", cookies=receiver_cookies).json()
        assert receiver_statement["balance"] == 50.0, (
            f"Receiver balance should be R$50, got {receiver_statement['balance']}"
        )

    def test_camera_qr_double_payment_rejected(
        self, payer_token: str, receiver_token: str
    ) -> None:
        """
        Paying the same QR Code twice must return 409 Conflict on the second attempt.
        """
        receiver_cookies = {"access_token": f"Bearer {receiver_token}"}
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        # Generate new charge
        charge_resp = client.post(
            "/pix/cobrar",
            json={"value": 10.0, "description": "Double pay test"},
            cookies=receiver_cookies,
        )
        assert charge_resp.status_code == 200
        charge_id = charge_resp.json()["charge_id"]
        emv = _build_emv(charge_id)

        # First payment
        resp1 = client.post(
            "/pix/qrcode/pagar",
            json={"payload": emv},
            headers={"X-Idempotency-Key": f"double-1-{charge_id}"},
            cookies=payer_cookies,
        )
        assert resp1.status_code == 200

        # Second payment of the same charge — must be rejected
        resp2 = client.post(
            "/pix/qrcode/pagar",
            json={"payload": emv},
            headers={"X-Idempotency-Key": f"double-2-{charge_id}"},
            cookies=payer_cookies,
        )
        assert resp2.status_code == 409, (
            f"Expected 409 for duplicate payment, got {resp2.status_code}: {resp2.json()}"
        )

    def test_camera_qr_idempotency_no_double_debit(
        self, payer_token: str, receiver_token: str
    ) -> None:
        """
        Sending the same request twice with the same X-Idempotency-Key must
        return the existing transaction without debiting balance a second time.
        """
        receiver_cookies = {"access_token": f"Bearer {receiver_token}"}
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        # Get payer balance before
        balance_before = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]

        charge_resp = client.post(
            "/pix/cobrar",
            json={"value": 5.0, "description": "Idempotency test"},
            cookies=receiver_cookies,
        )
        assert charge_resp.status_code == 200
        charge_id = charge_resp.json()["charge_id"]
        emv = _build_emv(charge_id)
        idem_key = f"idem-{charge_id}"

        # First call
        resp1 = client.post(
            "/pix/qrcode/pagar",
            json={"payload": emv},
            headers={"X-Idempotency-Key": idem_key},
            cookies=payer_cookies,
        )
        assert resp1.status_code == 200

        # Second call — same idempotency key, returns cached result
        resp2 = client.post(
            "/pix/qrcode/pagar",
            json={"payload": emv},
            headers={"X-Idempotency-Key": idem_key},
            cookies=payer_cookies,
        )
        assert resp2.status_code == 200

        # Balance must have been debited only ONCE (value R$5 + taxa de manutencao R$1)
        balance_after = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]
        assert balance_after == balance_before - 6.0, (
            f"Expected balance {balance_before - 6.0}, got {balance_after}. "
            "Idempotency failure: balance debited twice."
        )


# ===========================================================================
# 2. COPIA E COLA — internal and external
# ===========================================================================

class TestCopiaECola:
    """
    Simulates the user pasting a 'Copia e Cola' code into the payment field.
    Covers both internal (simulation UUID embedded in EMV) and
    external codes (no UUID — must go to Asaas, mocked here).
    """

    def test_copia_e_cola_internal_charge(
        self, payer_token: str, receiver_token: str
    ) -> None:
        """
        Copia e Cola with EMV containing an internal charge UUID.
        Same Routing 1a path as camera, but representative of the paste UX.
        """
        receiver_cookies = {"access_token": f"Bearer {receiver_token}"}
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        # Receiver generates charge
        charge_resp = client.post(
            "/pix/cobrar",
            json={"value": 25.0, "description": "Copia e Cola internal test"},
            cookies=receiver_cookies,
        )
        assert charge_resp.status_code == 200
        charge_id = charge_resp.json()["charge_id"]
        copy_paste_code = charge_resp.json()["copy_and_paste"]

        # Payer pastes the code
        pay_resp = client.post(
            "/pix/qrcode/pagar",
            json={"payload": copy_paste_code, "description": "Copia e Cola paste"},
            headers={"X-Idempotency-Key": f"copiacola-{charge_id}"},
            cookies=payer_cookies,
        )
        assert pay_resp.status_code == 200, f"copia e cola failed: {pay_resp.json()}"
        data = pay_resp.json()

        assert data["status"] == "CONFIRMADO"
        assert data["value"] == 25.0
        assert data["type"] == "ENVIADO"

    def test_copia_e_cola_emv_field54_value_parsed(
        self, payer_token: str, receiver_token: str
    ) -> None:
        """
        EMV field 54 (Transaction Amount) correctly extracted by _parse_emv_value.
        Verifies R$30.00 encoded as '540530.00' is read and applied correctly.
        """
        receiver_cookies = {"access_token": f"Bearer {receiver_token}"}
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        charge_resp = client.post(
            "/pix/cobrar",
            json={"value": 30.0, "description": "EMV field54 test"},
            cookies=receiver_cookies,
        )
        assert charge_resp.status_code == 200
        charge_id = charge_resp.json()["charge_id"]

        # Build EMV with field 54 encoding R$30.00
        # field 54: tag=54, length=05, value=30.00 → "540530.00"
        emv_with_field54 = _build_emv(charge_id, value=30.0)
        assert "540530.00" in emv_with_field54, (
            f"EMV field 54 not correctly encoded: {emv_with_field54}"
        )

        pay_resp = client.post(
            "/pix/qrcode/pagar",
            json={"payload": emv_with_field54, "value": 30.0, "description": "Field 54 test"},
            headers={"X-Idempotency-Key": f"field54-{charge_id}"},
            cookies=payer_cookies,
        )
        assert pay_resp.status_code == 200, f"field54 payment failed: {pay_resp.json()}"
        assert pay_resp.json()["value"] == 30.0

    def test_copia_e_cola_external_asaas_debits_balance(
        self, payer_token: str
    ) -> None:
        """
        Copia e Cola with an external EMV (no internal UUID).
        Asaas gateway is mocked. Verifies balance is debited after successful dispatch.
        """
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        # Balance snapshot before
        balance_before = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]
        assert balance_before > 0, "Payer has no balance to test external payment"

        external_emv = (
            "00020126360014BR.GOV.BCB.PIX0114+5511999990001"
            "52040000530398654075.005802BR5925External Merchant"
            "6009SAO PAULO62070503***6304CAFE"
        )

        mock_result = {
            "payment_id": "pay_ext_mock_001",
            "status": "AWAITING_TRANSFER_AUTHORIZATION",
            "value": 75.0,
            "end_to_end_id": "E0000000000000000000000000000001",
            "receiver_name": "External Merchant",
        }

        with patch("app.pix.router.get_payment_gateway") as mock_gw_factory:
            mock_gw = MagicMock()
            mock_gw.pay_qr_code.return_value = mock_result
            mock_gw_factory.return_value = mock_gw

            pay_resp = client.post(
                "/pix/qrcode/pagar",
                json={
                    "payload": external_emv,
                    "value": 75.0,
                    "description": "External Asaas payment",
                },
                headers={"X-Idempotency-Key": f"ext-asaas-{uuid4()}"},
                cookies=payer_cookies,
            )

        assert pay_resp.status_code == 200, (
            f"External Asaas copia e cola failed: {pay_resp.json()}"
        )
        data = pay_resp.json()
        assert data["value"] == 75.0

        # Balance must be debited: value R$75 + fee R$4 (rede+manutencao) = R$79 total
        balance_after = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]
        expected_debit = 79.0  # R$75 value + R$4 platform fee
        assert abs((balance_before - balance_after) - expected_debit) < 0.01, (
            f"Balance not debited after external payment. "
            f"Before: {balance_before}, After: {balance_after}, Expected debit: R${expected_debit:.2f}"
        )


# ===========================================================================
# 3. CHAVE ALEATORIA (random EVP code) — external via Asaas
# ===========================================================================

class TestChaveAleatoria:
    """
    Tests payment with a random EVP key (chave aleatoria) — 32-char UUID-like string
    with no internal BioCodeTechPay charge embedded. Always goes to Asaas (Routing 2).
    """

    def test_chave_aleatoria_dispatched_to_asaas(self, payer_token: str) -> None:
        """
        Random EVP code has no UUID in the payload — must be dispatched to Asaas.
        Gateway is mocked to return a successful result.
        Balance must be debited.
        """
        payer_cookies = {"access_token": f"Bearer {payer_token}"}
        balance_before = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]

        evp_payload = (
            "00020126520014BR.GOV.BCB.PIX0130a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            "5204000053039865404"
            "20.005802BR5916Test Recipient6008SAO PAULO62070503***6304BEEF"
        )

        mock_result = {
            "payment_id": "pay_evp_mock_002",
            "status": "AWAITING_TRANSFER_AUTHORIZATION",
            "value": 20.0,
            "end_to_end_id": "E0000000000000000000000000000002",
            "receiver_name": "Test Recipient",
        }

        with patch("app.pix.router.get_payment_gateway") as mock_gw_factory:
            mock_gw = MagicMock()
            mock_gw.pay_qr_code.return_value = mock_result
            mock_gw_factory.return_value = mock_gw

            pay_resp = client.post(
                "/pix/qrcode/pagar",
                json={
                    "payload": evp_payload,
                    "value": 20.0,
                    "description": "Chave aleatoria test",
                },
                headers={"X-Idempotency-Key": f"evp-{uuid4()}"},
                cookies=payer_cookies,
            )

        assert pay_resp.status_code == 200, (
            f"EVP payment failed: {pay_resp.json()}"
        )
        assert pay_resp.json()["value"] == 20.0

        balance_after = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]
        expected_debit = 24.0  # R$20 value + R$4 platform fee (rede+manutencao)
        assert abs((balance_before - balance_after) - expected_debit) < 0.01, (
            f"Balance not debited for EVP payment. "
            f"Before: {balance_before}, After: {balance_after}, Expected debit: R${expected_debit:.2f}"
        )

    def test_chave_aleatoria_asaas_error_returns_422(self, payer_token: str) -> None:
        """
        When Asaas returns an error for the EVP code, the endpoint must
        propagate 422 with the Asaas error message. Balance must NOT be debited.
        """
        payer_cookies = {"access_token": f"Bearer {payer_token}"}
        balance_before = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]

        bad_evp_payload = (
            "00020126520014BR.GOV.BCB.PIX0130ffffffff-0000-0000-0000-000000000000"
            "52040000530398654025.005802BR5916Bad Recipient6008SAO PAULO62070503***6304DEAD"
        )

        with patch("app.pix.router.get_payment_gateway") as mock_gw_factory:
            mock_gw = MagicMock()
            mock_gw.pay_qr_code.side_effect = Exception("Invalid QR Code or beneficiary not found")
            mock_gw_factory.return_value = mock_gw

            pay_resp = client.post(
                "/pix/qrcode/pagar",
                json={
                    "payload": bad_evp_payload,
                    "value": 25.0,
                    "description": "Invalid EVP test",
                },
                headers={"X-Idempotency-Key": f"evp-err-{uuid4()}"},
                cookies=payer_cookies,
            )

        assert pay_resp.status_code == 422, (
            f"Expected 422 on Asaas error, got {pay_resp.status_code}: {pay_resp.json()}"
        )

        # Balance must NOT have changed
        balance_after = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]
        assert balance_after == balance_before, (
            f"Balance was modified despite Asaas error. Before: {balance_before}, After: {balance_after}"
        )

    def test_chave_aleatoria_no_gateway_returns_503(self, payer_token: str) -> None:
        """
        When the gateway is unavailable (None), endpoint must return 503.
        """
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        evp_payload = (
            "00020126520014BR.GOV.BCB.PIX0130bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
            "52040000530398654015.005802BR5910No Gateway6008SAO PAULO62070503***6304CAFE"
        )

        with patch("app.pix.router.get_payment_gateway", return_value=None):
            resp = client.post(
                "/pix/qrcode/pagar",
                json={"payload": evp_payload, "value": 15.0},
                headers={"X-Idempotency-Key": f"evp-503-{uuid4()}"},
                cookies=payer_cookies,
            )

        assert resp.status_code == 503, (
            f"Expected 503 when gateway unavailable, got {resp.status_code}"
        )


# ===========================================================================
# 4. GUARDS — balance, validation, self-deposit
# ===========================================================================

class TestQrCodeGuards:
    """
    Validates all protective guardrails of POST /pix/qrcode/pagar.
    """

    def _create_user_with_balance(
        self, cpf: str, email: str, balance: float
    ) -> str:
        """Helper: creates a user with exact balance and returns access token."""
        db = TestingSessionLocal()
        existing = db.query(User).filter(User.cpf_cnpj == cpf).first()
        if not existing:
            user = User(
                name=f"Guard Test {cpf}",
                email=email,
                cpf_cnpj=cpf,
                hashed_password=get_password_hash("pass123"),
                balance=balance,
                credit_limit=0.0,
                email_verified=True,
            )
            db.add(user)
            db.commit()
        db.close()

        resp = client.post("/auth/login", json={"cpf_cnpj": cpf, "password": "pass123"})
        assert resp.status_code == 200, f"Login failed for {cpf}: {resp.json()}"
        return resp.json()["access_token"]

    def test_zero_balance_blocks_external_payment(self) -> None:
        """
        User with R$0 balance trying to pay an external QR Code must get 400.
        """
        token = self._create_user_with_balance("77700000010", "zero@test.com", 0.0)
        cookies = {"access_token": f"Bearer {token}"}

        # External payload (no internal UUID) → Routing 2 → zero balance check fires
        external_payload = (
            "00020126360014BR.GOV.BCB.PIX0114+5511988880000"
            "52040000530398654015.005802BR5910Merchant6008SAO PAULO62070503***6304ABCD"
        )

        with patch("app.pix.router.get_payment_gateway") as mock_gw_f:
            mock_gw_f.return_value = MagicMock()
            resp = client.post(
                "/pix/qrcode/pagar",
                json={"payload": external_payload, "value": 15.0},
                headers={"X-Idempotency-Key": f"zero-{uuid4()}"},
                cookies=cookies,
            )

        assert resp.status_code == 400, (
            f"Expected 400 for zero balance, got {resp.status_code}: {resp.json()}"
        )
        assert "Saldo insuficiente" in resp.json()["detail"]

    def test_insufficient_balance_for_internal_charge(
        self, receiver_token: str
    ) -> None:
        """
        User with R$1 balance cannot pay an internal charge of R$100.
        """
        token = self._create_user_with_balance("77700000011", "broke@test.com", 1.0)
        payer_cookies = {"access_token": f"Bearer {token}"}
        receiver_cookies = {"access_token": f"Bearer {receiver_token}"}

        # Receiver generates charge of R$100
        charge_resp = client.post(
            "/pix/cobrar",
            json={"value": 100.0, "description": "Insufficient balance test"},
            cookies=receiver_cookies,
        )
        assert charge_resp.status_code == 200
        charge_id = charge_resp.json()["charge_id"]
        emv = _build_emv(charge_id)

        pay_resp = client.post(
            "/pix/qrcode/pagar",
            json={"payload": emv},
            headers={"X-Idempotency-Key": f"insuf-{charge_id}"},
            cookies=payer_cookies,
        )
        assert pay_resp.status_code == 400, (
            f"Expected 400 for insufficient balance, got {pay_resp.status_code}: {pay_resp.json()}"
        )
        assert "Saldo insuficiente" in pay_resp.json()["detail"]

    def test_self_deposit_no_balance_debit(self) -> None:
        """
        When the payer is also the receiver (self-deposit), balance must
        NOT be debited — the charge is simply confirmed.
        """
        token = self._create_user_with_balance("77700000012", "self@test.com", 0.0)
        cookies = {"access_token": f"Bearer {token}"}

        # User creates a charge for themselves
        charge_resp = client.post(
            "/pix/cobrar",
            json={"value": 200.0, "description": "Self deposit test"},
            cookies=cookies,
        )
        assert charge_resp.status_code == 200
        charge_id = charge_resp.json()["charge_id"]
        emv = _build_emv(charge_id)

        # User pays their own charge (self-deposit)
        pay_resp = client.post(
            "/pix/qrcode/pagar",
            json={"payload": emv, "description": "Self deposit"},
            headers={"X-Idempotency-Key": f"self-{charge_id}"},
            cookies=cookies,
        )
        assert pay_resp.status_code == 200, f"Self deposit failed: {pay_resp.json()}"

        # Balance must reflect the credit (deposit), not a debit
        statement = client.get("/pix/extrato", cookies=cookies).json()
        assert statement["balance"] == 200.0, (
            f"Self-deposit should credit R$200, balance is {statement['balance']}"
        )

    def test_payload_too_short_returns_422(self, payer_token: str) -> None:
        """
        Payload shorter than 20 characters must fail schema validation (422).
        """
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        resp = client.post(
            "/pix/qrcode/pagar",
            json={"payload": "TOOSHORT"},
            cookies=payer_cookies,
        )
        assert resp.status_code == 422, (
            f"Expected 422 for short payload, got {resp.status_code}"
        )

    def test_unauthenticated_request_returns_401(self) -> None:
        """
        Request without auth token must be rejected with 401.
        """
        resp = client.post(
            "/pix/qrcode/pagar",
            json={"payload": "00020126360014BR.GOV.BCB.PIX0114+55119999900015204000053039865302BRxxxxxx"},
        )
        assert resp.status_code in (401, 403), (
            f"Expected 401/403 for unauthenticated request, got {resp.status_code}"
        )


# ===========================================================================
# 5. VALUE FALLBACK — Financial integrity when Asaas response has no value
# ===========================================================================

class TestValueFallback:
    """
    Tests the financial integrity fallback: when gateway.pay_qr_code() succeeds
    but returns no value field (AWAITING_TRANSFER_AUTHORIZATION on dynamic QR without
    EMV field-54), the endpoint must use data.value (from preceding /consultar step)
    as last resort to ensure the transaction is recorded and balance is debited.

    Without this fallback, the Asaas account would be debited but BioCodeTechPay
    would have no record and no internal balance deduction (orphaned payment).
    """

    def test_value_fallback_uses_client_value_when_asaas_returns_none(
        self, payer_token: str
    ) -> None:
        """
        Gateway succeeds (payment processed) but returns value=None.
        EMV payload has no field-54 (dynamic QR — value not embedded).
        data.value provided by client (from /consultar step) must be used.
        Balance must be debited by value + fee.
        """
        payer_cookies = {"access_token": f"Bearer {payer_token}"}
        balance_before = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]
        assert balance_before > 0, "Payer must have balance for this test"

        # Dynamic QR without field-54 (no `5404` tag) — _parse_emv_value returns 0
        dynamic_qr_no_value = (
            "00020126580014BR.GOV.BCB.PIX"
            "0136a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            "0225https://pix.example.com/cobv/abc"
            "5204000053039865802BR"
            "5913Merchant Name"
            "6009SAO PAULO"
            "62070503***"
            "6304CAFE"
        )
        payment_value = 4.90

        mock_result = {
            "payment_id": "pay_fallback_test_001",
            "status": "BANK_PROCESSING",
            "value": None,  # Asaas did not return value in response
            "end_to_end_id": None,
            "receiver_name": "Merchant Name",
        }

        with patch("app.pix.router.get_payment_gateway") as mock_gw_factory:
            mock_gw = MagicMock()
            mock_gw.pay_qr_code.return_value = mock_result
            mock_gw_factory.return_value = mock_gw

            resp = client.post(
                "/pix/qrcode/pagar",
                json={
                    "payload": dynamic_qr_no_value,
                    "value": payment_value,
                    "description": "Fallback value test",
                },
                headers={"X-Idempotency-Key": f"fallback-{uuid4()}"},
                cookies=payer_cookies,
            )

        assert resp.status_code == 200, (
            f"Expected 200 when value resolved via fallback, got {resp.status_code}: {resp.json()}"
        )
        data = resp.json()
        assert data["value"] == payment_value, (
            f"Transaction value should be {payment_value}, got {data['value']}"
        )

        # Balance must be debited (value + platform fee R$4.00)
        balance_after = client.get("/pix/extrato", cookies=payer_cookies).json()["balance"]
        expected_debit = payment_value + 4.0  # R$4.90 value + R$4 platform fee = R$8.90
        assert abs((balance_before - balance_after) - expected_debit) < 0.01, (
            f"Balance not debited correctly. Before: {balance_before}, After: {balance_after}, "
            f"Expected debit: R${expected_debit:.2f}"
        )

    def test_no_fallback_without_client_value_returns_422(
        self, payer_token: str
    ) -> None:
        """
        Gateway succeeds but returns value=None, EMV has no field-54, and
        client provides no data.value. Must return 422 — payment genuinely
        unresolvable. This case should never happen in normal flow where
        /consultar is called before /pagar.
        """
        payer_cookies = {"access_token": f"Bearer {payer_token}"}

        dynamic_qr_no_value = (
            "00020126580014BR.GOV.BCB.PIX"
            "0136b2c3d4e5-f6a7-8901-bcde-f12345678901"
            "0225https://pix.example.com/cobv/xyz"
            "52040000530398"
            "5802BR"
            "5913Another Merch"
            "6009SAO PAULO"
            "62070503***"
            "63040000"
        )

        mock_result = {
            "payment_id": "pay_no_fallback_001",
            "status": "BANK_PROCESSING",
            "value": None,
            "end_to_end_id": None,
            "receiver_name": "",
        }

        with patch("app.pix.router.get_payment_gateway") as mock_gw_factory:
            mock_gw = MagicMock()
            mock_gw.pay_qr_code.return_value = mock_result
            mock_gw_factory.return_value = mock_gw

            resp = client.post(
                "/pix/qrcode/pagar",
                json={
                    "payload": dynamic_qr_no_value,
                    # data.value intentionally omitted
                    "description": "No value test",
                },
                headers={"X-Idempotency-Key": f"no-fallback-{uuid4()}"},
                cookies=payer_cookies,
            )

        assert resp.status_code == 422, (
            f"Expected 422 when value cannot be determined and no client fallback, "
            f"got {resp.status_code}: {resp.json()}"
        )
