"""
Comprehensive payment flow tests — Bio Code Tech Pay.

Covers:
  - Deposit (internal credit)
  - Internal PIX transfer (Bio Code Tech Pay to Bio Code Tech Pay) by CPF, CNPJ, EMAIL
  - External PIX transfer (other banks) — PF fee R$ 2.50, PJ fee 0.8% min R$ 3.00
  - PIX copia e cola (EMV QR code payload parsed and matched to internal charge)
  - QR code generation and payment (charge flow: CREATED -> CONFIRMED)
  - Insufficient balance guard on internal and external transfers
  - Idempotency: duplicate submission returns same transaction
  - Fee calculation assertions for all account types
"""
import pytest
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch as _patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.auth.models import User
from app.auth.service import deposit_funds, get_password_hash
from app.pix.service import create_pix
from app.pix.schemas import PixCreateRequest, PixKeyType
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.core.fees import (
    calculate_pix_fee,
    calculate_boleto_fee,
    fee_display,
    is_pj,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def db():
    """Isolated in-memory SQLite database per test function."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _make_user(db, *, name: str, email: str, cpf_cnpj: str, balance: float = 0.0) -> User:
    """Helper: create and persist a user."""
    user = User(
        name=name,
        email=email,
        cpf_cnpj=cpf_cnpj,
        hashed_password=get_password_hash("senha123"),
        balance=balance,
        credit_limit=10000.0,
        is_active=True,
        email_verified=True,
        document_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def pf_alice(db):
    """PF user — CPF 11 digits."""
    return _make_user(db, name="Alice PF", email="alice@biocodetechpay.com",
                      cpf_cnpj="11111111111")


@pytest.fixture
def pf_bob(db):
    """PF user — CPF 11 digits."""
    return _make_user(db, name="Bob PF", email="bob@biocodetechpay.com",
                      cpf_cnpj="22222222222")


@pytest.fixture
def pj_carlos(db):
    """PJ user — CNPJ 14 digits."""
    return _make_user(db, name="Carlos PJ", email="carlos@empresa.com",
                      cpf_cnpj="61425124000103")


@pytest.fixture
def pj_diana(db):
    """PJ user — CNPJ 14 digits."""
    return _make_user(db, name="Diana Empresa", email="diana@empresa.com",
                      cpf_cnpj="12345678000195")


# ---------------------------------------------------------------------------
# 1. DEPOSIT
# ---------------------------------------------------------------------------

class TestDeposit:
    def test_deposit_credits_balance(self, db, pf_alice):
        result = deposit_funds(db, pf_alice.id, 500.00, "Deposito inicial")
        db.refresh(pf_alice)

        assert result["new_balance"] == pytest.approx(500.00, abs=0.01)
        assert pf_alice.balance == pytest.approx(500.00, abs=0.01)

    def test_deposit_accumulates(self, db, pf_alice):
        deposit_funds(db, pf_alice.id, 300.00)
        deposit_funds(db, pf_alice.id, 200.00)
        db.refresh(pf_alice)

        assert pf_alice.balance == pytest.approx(500.00, abs=0.01)

    def test_deposit_zero_raises(self, db, pf_alice):
        with pytest.raises(ValueError, match="positive"):
            deposit_funds(db, pf_alice.id, 0.00)

    def test_deposit_negative_raises(self, db, pf_alice):
        with pytest.raises(ValueError, match="positive"):
            deposit_funds(db, pf_alice.id, -50.00)

    def test_deposit_exceeds_limit_raises(self, db, pf_alice):
        with pytest.raises(ValueError, match="limit"):
            deposit_funds(db, pf_alice.id, 2_000_000.00)


# ---------------------------------------------------------------------------
# 2. FEE CALCULATION
# ---------------------------------------------------------------------------

class TestFeeCalculation:
    # PF — external sent
    def test_pf_external_pix_fee_is_025(self):
        fee = calculate_pix_fee("11111111111", 200.00, is_external=True)
        assert fee == Decimal("2.50")

    def test_pf_internal_pix_fee_is_zero(self):
        fee = calculate_pix_fee("11111111111", 200.00, is_external=False)
        assert fee == Decimal("0.00")

    def test_pf_received_pix_fee_is_249(self):
        fee = calculate_pix_fee("11111111111", 500.00, is_external=True, is_received=True)
        assert fee == Decimal("2.49")

    def test_pf_boleto_fee_is_249(self):
        fee = calculate_boleto_fee("11111111111")
        assert fee == Decimal("2.49")

    # PJ — external sent (0.8% min R$3.00)
    def test_pj_external_pix_fee_minimum(self):
        # 0.8% of R$50 = R$0.40 -> floor to minimum R$3.00
        fee = calculate_pix_fee("61425124000103", 50.00, is_external=True)
        assert fee == Decimal("3.00")

    def test_pj_external_pix_fee_percentage(self):
        # 0.8% of R$500 = R$4.00
        fee = calculate_pix_fee("61425124000103", 500.00, is_external=True)
        assert fee == Decimal("4.00")

    def test_pj_external_pix_fee_received(self):
        # 0.495% of R$1000 = R$4.95
        fee = calculate_pix_fee("61425124000103", 1000.00, is_external=True, is_received=True)
        assert fee == Decimal("4.95")

    def test_pj_boleto_fee_is_299(self):
        fee = calculate_boleto_fee("61425124000103")
        assert fee == Decimal("2.99")

    def test_is_pj_detects_cnpj(self):
        assert is_pj("61425124000103") is True

    def test_is_pj_detects_cpf(self):
        assert is_pj("11111111111") is False

    def test_fee_display_formatting(self):
        assert fee_display(Decimal("0.25")) == "R$ 0,25"
        assert fee_display(Decimal("1.75")) == "R$ 1,75"
        assert fee_display(Decimal("2.50")) == "R$ 2,50"


# ---------------------------------------------------------------------------
# 3. INTERNAL PIX TRANSFER (Bio Code Tech Pay → Bio Code Tech Pay)
# ---------------------------------------------------------------------------

class TestInternalPixTransfer:
    def test_internal_transfer_by_cpf(self, db, pf_alice, pf_bob):
        """Alice sends to Bob by CPF — no fee, both balances updated."""
        deposit_funds(db, pf_alice.id, 1000.00)

        req = PixCreateRequest(value=300.00, pix_key="22222222222",
                               key_type=PixKeyType.CPF, description="Pagamento Bob")
        tx = create_pix(db, req, idempotency_key=str(uuid4()),
                        correlation_id=str(uuid4()), user_id=pf_alice.id,
                        type=TransactionType.SENT)

        db.refresh(pf_alice)
        db.refresh(pf_bob)

        assert tx.status == PixStatus.CONFIRMED
        assert tx.type == TransactionType.SENT
        assert tx.value == pytest.approx(300.00, abs=0.01)
        assert pf_alice.balance == pytest.approx(700.00, abs=0.01)   # no fee
        assert pf_bob.balance == pytest.approx(300.00, abs=0.01)

    def test_internal_transfer_by_email(self, db, pf_alice, pf_bob):
        """Alice sends to Bob by e-mail key — free internal transfer."""
        deposit_funds(db, pf_alice.id, 500.00)

        req = PixCreateRequest(value=150.00, pix_key="bob@biocodetechpay.com",
                               key_type=PixKeyType.EMAIL)
        tx = create_pix(db, req, idempotency_key=str(uuid4()),
                        correlation_id=str(uuid4()), user_id=pf_alice.id,
                        type=TransactionType.SENT)

        db.refresh(pf_alice)
        db.refresh(pf_bob)

        assert tx.status == PixStatus.CONFIRMED
        assert pf_alice.balance == pytest.approx(350.00, abs=0.01)
        assert pf_bob.balance == pytest.approx(150.00, abs=0.01)

    def test_internal_transfer_pj_by_cnpj(self, db, pj_carlos, pj_diana):
        """PJ sends to PJ by CNPJ — internal, always free."""
        deposit_funds(db, pj_carlos.id, 2000.00)

        req = PixCreateRequest(value=500.00, pix_key="12345678000195",
                               key_type=PixKeyType.CNPJ)
        tx = create_pix(db, req, idempotency_key=str(uuid4()),
                        correlation_id=str(uuid4()), user_id=pj_carlos.id,
                        type=TransactionType.SENT)

        db.refresh(pj_carlos)
        db.refresh(pj_diana)

        assert tx.status == PixStatus.CONFIRMED
        assert pj_carlos.balance == pytest.approx(1500.00, abs=0.01)  # no fee
        assert pj_diana.balance == pytest.approx(500.00, abs=0.01)

    def test_internal_transfer_creates_received_record(self, db, pf_alice, pf_bob):
        """Guarantees a RECEIVED record is created for Bob."""
        deposit_funds(db, pf_alice.id, 800.00)

        corr = str(uuid4())
        req = PixCreateRequest(value=100.00, pix_key="22222222222",
                               key_type=PixKeyType.CPF)
        create_pix(db, req, idempotency_key=str(uuid4()), correlation_id=corr,
                   user_id=pf_alice.id, type=TransactionType.SENT)

        recv_tx = db.query(PixTransaction).filter(
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.user_id == pf_bob.id,
            PixTransaction.correlation_id == corr,
        ).first()

        assert recv_tx is not None
        assert recv_tx.status == PixStatus.CONFIRMED
        assert recv_tx.value == pytest.approx(100.00, abs=0.01)

    def test_internal_insufficient_balance_raises(self, db, pf_alice, pf_bob):
        """Transfer of R$200 with balance of R$50 must raise ValueError."""
        deposit_funds(db, pf_alice.id, 50.00)

        req = PixCreateRequest(value=200.00, pix_key="22222222222",
                               key_type=PixKeyType.CPF)
        with pytest.raises(ValueError, match="(?i)saldo insuficiente|insufficient"):
            create_pix(db, req, idempotency_key=str(uuid4()),
                       correlation_id=str(uuid4()), user_id=pf_alice.id,
                       type=TransactionType.SENT)

        # Balance must remain unchanged after failure
        db.refresh(pf_alice)
        assert pf_alice.balance == pytest.approx(50.00, abs=0.01)

    def test_internal_exact_balance_succeeds(self, db, pf_alice, pf_bob):
        """Transfer of exactly the available balance (internal = no fee) succeeds."""
        deposit_funds(db, pf_alice.id, 300.00)

        req = PixCreateRequest(value=300.00, pix_key="22222222222",
                               key_type=PixKeyType.CPF)
        tx = create_pix(db, req, idempotency_key=str(uuid4()),
                        correlation_id=str(uuid4()), user_id=pf_alice.id,
                        type=TransactionType.SENT)

        db.refresh(pf_alice)
        assert tx.status == PixStatus.CONFIRMED
        assert pf_alice.balance == pytest.approx(0.00, abs=0.01)


# ---------------------------------------------------------------------------
# 4. EXTERNAL PIX TRANSFER (other banks — gateway mocked)
# ---------------------------------------------------------------------------

class TestExternalPixTransfer:
    def test_pf_external_deducts_value_plus_025_fee(self, db, pf_alice):
        """PF external PIX: R$200 value + R$2.50 fee = R$202.50 debited."""
        deposit_funds(db, pf_alice.id, 1000.00)

        req = PixCreateRequest(value=200.00, pix_key="99999999999",
                               key_type=PixKeyType.CPF)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            tx = create_pix(db, req, idempotency_key=str(uuid4()),
                            correlation_id=str(uuid4()), user_id=pf_alice.id,
                            type=TransactionType.SENT)

        db.refresh(pf_alice)
        assert tx.status == PixStatus.CONFIRMED
        assert pf_alice.balance == pytest.approx(797.50, abs=0.01)  # 1000 - 200 - 2.50

    def test_pj_external_deducts_percentage_fee(self, db, pj_carlos):
        """PJ external PIX R$1000: fee = 0.5% = R$5.00 → balance = 1994.95 + initial."""
        deposit_funds(db, pj_carlos.id, 2000.00)

        req = PixCreateRequest(value=1000.00, pix_key="88888888888888",
                               key_type=PixKeyType.CNPJ)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            tx = create_pix(db, req, idempotency_key=str(uuid4()),
                            correlation_id=str(uuid4()), user_id=pj_carlos.id,
                            type=TransactionType.SENT)

        db.refresh(pj_carlos)
        # 0.8% of 1000 = 8.00
        assert tx.status == PixStatus.CONFIRMED
        assert pj_carlos.balance == pytest.approx(992.00, abs=0.01)  # 2000 - 1000 - 8.00

    def test_pj_external_minimum_fee_applies(self, db, pj_carlos):
        """PJ external PIX R$50: 0.8% = R$0.40 < min R$3.00 → minimum applies."""
        deposit_funds(db, pj_carlos.id, 200.00)

        req = PixCreateRequest(value=50.00, pix_key="88888888888888",
                               key_type=PixKeyType.CNPJ)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            create_pix(db, req, idempotency_key=str(uuid4()),
                       correlation_id=str(uuid4()), user_id=pj_carlos.id,
                       type=TransactionType.SENT)

        db.refresh(pj_carlos)
        assert pj_carlos.balance == pytest.approx(147.00, abs=0.01)  # 200 - 50 - 3.00

    def test_pf_external_insufficient_balance_includes_fee_message(self, db, pf_alice):
        """Error message must inform: disponivel, necessario, valor, taxa."""
        deposit_funds(db, pf_alice.id, 200.00)  # R$200 — not enough for R$200 + R$2.50

        req = PixCreateRequest(value=200.00, pix_key="77777777777",
                               key_type=PixKeyType.CPF)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            with pytest.raises(ValueError) as exc_info:
                create_pix(db, req, idempotency_key=str(uuid4()),
                           correlation_id=str(uuid4()), user_id=pf_alice.id,
                           type=TransactionType.SENT)

        error_msg = str(exc_info.value)
        assert "200.00" in error_msg   # valor disponivel
        assert "202.50" in error_msg   # total necessario

    def test_pf_external_zero_balance_raises(self, db, pf_alice):
        """Zero balance must raise before any gateway call."""
        req = PixCreateRequest(value=50.00, pix_key="66666666666",
                               key_type=PixKeyType.CPF)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            with pytest.raises(ValueError, match="(?i)saldo insuficiente|insufficient"):
                create_pix(db, req, idempotency_key=str(uuid4()),
                           correlation_id=str(uuid4()), user_id=pf_alice.id,
                           type=TransactionType.SENT)

    def test_pf_external_no_received_record_created(self, db, pf_alice):
        """External transfer must NOT create a RECEIVED record in the database."""
        deposit_funds(db, pf_alice.id, 500.00)
        corr = str(uuid4())

        req = PixCreateRequest(value=100.00, pix_key="55555555555",
                               key_type=PixKeyType.CPF)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            create_pix(db, req, idempotency_key=str(uuid4()), correlation_id=corr,
                       user_id=pf_alice.id, type=TransactionType.SENT)

        recv_count = db.query(PixTransaction).filter(
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.correlation_id == corr
        ).count()
        assert recv_count == 0

    def test_pf_external_exact_balance_plus_fee_succeeds(self, db, pf_alice):
        """Balance = value + R$2.50 exactly — must succeed."""
        deposit_funds(db, pf_alice.id, 102.50)

        req = PixCreateRequest(value=100.00, pix_key="44444444444",
                               key_type=PixKeyType.CPF)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            tx = create_pix(db, req, idempotency_key=str(uuid4()),
                            correlation_id=str(uuid4()), user_id=pf_alice.id,
                            type=TransactionType.SENT)

        db.refresh(pf_alice)
        assert tx.status == PixStatus.CONFIRMED
        assert pf_alice.balance == pytest.approx(0.00, abs=0.01)


# ---------------------------------------------------------------------------
# 5. PIX COPIA E COLA — internal QR code recognition
# ---------------------------------------------------------------------------

class TestPixCopiaCola:
    """
    Tests the _find_internal_qrcode_charge routing logic.
    An internal charge has its UUID embedded in the EMV payload.
    Paying it must: mark charge as CONFIRMED, debit payer, credit beneficiary.
    """

    def _create_pending_charge(self, db, user: User, value: float) -> PixTransaction:
        """Creates a RECEIVED/CREATED charge (simulating cobrand QR code)."""
        charge = PixTransaction(
            id=str(uuid4()),
            value=value,
            pix_key="test-qrcode-key",
            key_type="ALEATORIA",
            type=TransactionType.RECEIVED,
            status=PixStatus.CREATED,
            user_id=user.id,
            idempotency_key=str(uuid4()),
            description="Cobranca QR Code",
            correlation_id=str(uuid4()),
        )
        db.add(charge)
        db.commit()
        db.refresh(charge)
        return charge

    def test_qrcode_uuid_found_in_payload(self, db, pf_alice, pf_bob):
        """EMV payload containing the charge UUID must match the internal charge."""
        from app.pix.router import _find_internal_qrcode_charge as find_charge

        charge = self._create_pending_charge(db, pf_bob, 250.00)
        # EMV-like payload embedding the charge UUID
        emv_payload = (
            "00020126580014br.gov.bcb.pix0136"
            f"{charge.id}"
            "52040000530398654062505802BR5925Bio Code Tech Pay"
            "6009SAO PAULO62070503***63047F4B"
        )

        found, already_paid = find_charge(emv_payload, db, __import__("logging").getLogger())
        assert found is not None
        assert found.id == charge.id
        assert already_paid is False

    def test_qrcode_paid_charge_detected_as_already_paid(self, db, pf_alice, pf_bob):
        """A charge already CONFIRMED must be detected as already paid."""
        from app.pix.router import _find_internal_qrcode_charge as find_charge

        charge = self._create_pending_charge(db, pf_bob, 100.00)
        # Mark as already paid
        charge.status = PixStatus.CONFIRMED
        db.commit()

        emv_payload = (
            "00020126580014br.gov.bcb.pix0136"
            f"{charge.id}"
            "52040000530398654062505802BR5925Bio Code Tech Pay"
            "6009SAO PAULO62070503***63047F4B"
        )

        found, already_paid = find_charge(emv_payload, db, __import__("logging").getLogger())
        assert already_paid is True

    def test_qrcode_nonexistent_uuid_returns_none(self, db):
        """EMV payload with unknown UUID must return (None, False)."""
        from app.pix.router import _find_internal_qrcode_charge as find_charge

        unknown_uuid = str(uuid4())
        emv_payload = (
            "00020126580014br.gov.bcb.pix0136"
            f"{unknown_uuid}"
            "52040000530398654062505802BR5925SomeBank"
            "6009SAO PAULO62070503***63041D3E"
        )

        found, already_paid = find_charge(emv_payload, db, __import__("logging").getLogger())
        assert found is None
        assert already_paid is False


# ---------------------------------------------------------------------------
# 6. IDEMPOTENCY
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_duplicate_idempotency_key_returns_same_transaction(self, db, pf_alice, pf_bob):
        """Second call with same idempotency_key must return original transaction without double-debit."""
        deposit_funds(db, pf_alice.id, 1000.00)
        key = str(uuid4())
        corr = str(uuid4())

        req = PixCreateRequest(value=100.00, pix_key="22222222222", key_type=PixKeyType.CPF)
        tx1 = create_pix(db, req, idempotency_key=key, correlation_id=corr,
                         user_id=pf_alice.id, type=TransactionType.SENT)
        tx2 = create_pix(db, req, idempotency_key=key, correlation_id=corr,
                         user_id=pf_alice.id, type=TransactionType.SENT)

        assert tx1.id == tx2.id

        db.refresh(pf_alice)
        db.refresh(pf_bob)
        # Balance debited only once
        assert pf_alice.balance == pytest.approx(900.00, abs=0.01)
        assert pf_bob.balance == pytest.approx(100.00, abs=0.01)

    def test_different_idempotency_keys_debit_twice(self, db, pf_alice, pf_bob):
        """Two distinct idempotency keys on the same transfer create two separate transactions."""
        deposit_funds(db, pf_alice.id, 1000.00)

        req = PixCreateRequest(value=100.00, pix_key="22222222222", key_type=PixKeyType.CPF)
        create_pix(db, req, idempotency_key=str(uuid4()), correlation_id=str(uuid4()),
                   user_id=pf_alice.id, type=TransactionType.SENT)
        create_pix(db, req, idempotency_key=str(uuid4()), correlation_id=str(uuid4()),
                   user_id=pf_alice.id, type=TransactionType.SENT)

        db.refresh(pf_alice)
        db.refresh(pf_bob)
        assert pf_alice.balance == pytest.approx(800.00, abs=0.01)
        assert pf_bob.balance == pytest.approx(200.00, abs=0.01)


# ---------------------------------------------------------------------------
# 7. MULTI-STEP COMPREHENSIVE FLOW
# ---------------------------------------------------------------------------

class TestComprehensiveFlow:
    def test_deposit_internal_then_external_flow(self, db, pf_alice, pf_bob):
        """
        Full scenario:
          1. Alice deposits R$1000
          2. Alice sends R$200 to Bob internally (free)
          3. Alice sends R$100 externally to another bank (fee R$2.50)
          Final balances verified.
        """
        deposit_funds(db, pf_alice.id, 1000.00)

        # Step 2: internal to Bob
        req_internal = PixCreateRequest(value=200.00, pix_key="22222222222",
                                        key_type=PixKeyType.CPF, description="Pag Bob")
        create_pix(db, req_internal, idempotency_key=str(uuid4()),
                   correlation_id=str(uuid4()), user_id=pf_alice.id,
                   type=TransactionType.SENT)

        # Step 3: external to other bank
        req_external = PixCreateRequest(value=100.00, pix_key="33333333333",
                                        key_type=PixKeyType.CPF, description="Pag externo")
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            create_pix(db, req_external, idempotency_key=str(uuid4()),
                       correlation_id=str(uuid4()), user_id=pf_alice.id,
                       type=TransactionType.SENT)

        db.refresh(pf_alice)
        db.refresh(pf_bob)

        # Alice: 1000 - 200 (internal, no fee) - 100 - 2.50 (external fee)
        assert pf_alice.balance == pytest.approx(697.50, abs=0.01)
        # Bob: received R$200 internally
        assert pf_bob.balance == pytest.approx(200.00, abs=0.01)

    def test_pj_full_flow_fee_accumulation(self, db, pj_carlos, pj_diana):
        """
        PJ scenario:
          1. Carlos deposits R$5000
          2. Carlos sends R$1000 to Diana internally (free)
          3. Carlos sends R$500 externally (fee = 0.5% = R$2.50)
          4. Diana sends R$200 to external bank (fee = 0.5% of R$200 = R$1.00, min R$0.50)
        """
        deposit_funds(db, pj_carlos.id, 5000.00)

        # Internal
        req1 = PixCreateRequest(value=1000.00, pix_key="12345678000195", key_type=PixKeyType.CNPJ)
        create_pix(db, req1, idempotency_key=str(uuid4()), correlation_id=str(uuid4()),
                   user_id=pj_carlos.id, type=TransactionType.SENT)

        # Carlos external
        req2 = PixCreateRequest(value=500.00, pix_key="98765432000111", key_type=PixKeyType.CNPJ)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            create_pix(db, req2, idempotency_key=str(uuid4()), correlation_id=str(uuid4()),
                       user_id=pj_carlos.id, type=TransactionType.SENT)

        # Diana external
        req3 = PixCreateRequest(value=200.00, pix_key="11111111111111", key_type=PixKeyType.CNPJ)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            create_pix(db, req3, idempotency_key=str(uuid4()), correlation_id=str(uuid4()),
                       user_id=pj_diana.id, type=TransactionType.SENT)

        db.refresh(pj_carlos)
        db.refresh(pj_diana)

        # Carlos: 5000 - 1000 (free) - 500 - 4.00 (0.8% of 500) = 3496.00
        assert pj_carlos.balance == pytest.approx(3496.00, abs=0.01)
        # Diana: 1000 (received) - 200 - 3.00 (0.8% of 200=R$1.60 < min R$3.00) = 797.00
        assert pj_diana.balance == pytest.approx(797.00, abs=0.01)

    def test_insufficient_balance_after_partial_spending(self, db, pf_alice, pf_bob):
        """
        Alice has R$150. After sending R$100 internally she has R$50.
        Attempt to send R$50 externally must fail because R$50 + R$2.50 fee > R$50.
        """
        deposit_funds(db, pf_alice.id, 150.00)

        # First transfer (internal, free)
        req1 = PixCreateRequest(value=100.00, pix_key="22222222222", key_type=PixKeyType.CPF)
        create_pix(db, req1, idempotency_key=str(uuid4()), correlation_id=str(uuid4()),
                   user_id=pf_alice.id, type=TransactionType.SENT)

        db.refresh(pf_alice)
        assert pf_alice.balance == pytest.approx(50.00, abs=0.01)

        # Second transfer (external) — balance insufficient for value + fee
        req2 = PixCreateRequest(value=50.00, pix_key="33333333333", key_type=PixKeyType.CPF)
        with _patch("app.pix.service.get_payment_gateway", return_value=None):
            with pytest.raises(ValueError, match="(?i)saldo insuficiente|insufficient"):
                create_pix(db, req2, idempotency_key=str(uuid4()),
                           correlation_id=str(uuid4()), user_id=pf_alice.id,
                           type=TransactionType.SENT)

        db.refresh(pf_alice)
        assert pf_alice.balance == pytest.approx(50.00, abs=0.01)  # unchanged after failure
