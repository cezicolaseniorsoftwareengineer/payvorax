"""
Integration tests for internal PIX transfers.
Tests complete flow: deposit -> internal PIX -> balance validation.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.auth.models import User
from app.auth.service import deposit_funds, get_password_hash
from app.pix.service import create_pix
from app.pix.schemas import PixCreateRequest, PixKeyType
from app.pix.models import PixTransaction, PixStatus, TransactionType


@pytest.fixture(scope="function")
def db():
    """Creates in-memory database for tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def user_alice(db):
    """Creates user Alice."""
    user = User(
        name="Alice",
        email="alice@biocodetechpay.com",
        cpf_cnpj="11111111111",
        hashed_password=get_password_hash("alice123"),
        balance=0.0,
        credit_limit=5000.0
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def user_bob(db):
    """Creates user Bob."""
    user = User(
        name="Bob",
        email="bob@biocodetechpay.com",
        cpf_cnpj="22222222222",
        hashed_password=get_password_hash("bob123"),
        balance=0.0,
        credit_limit=5000.0
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_internal_pix_transfer_by_cpf(db, user_alice, user_bob):
    """
    Tests complete internal PIX transfer flow using CPF key.
    Flow: Alice deposits -> Alice sends PIX to Bob's CPF -> Both balances updated.
    """
    # Step 1: Alice deposits R$ 1000
    deposit_funds(db, user_alice.id, 1000.00, "Initial deposit")
    db.refresh(user_alice)
    assert user_alice.balance == 1000.00

    # Step 2: Alice sends R$ 300 to Bob via PIX (Bob's CPF)
    pix_request = PixCreateRequest(
        value=300.00,
        pix_key="22222222222",  # Bob's CPF
        key_type=PixKeyType.CPF,
        description="Payment to Bob"
    )

    sent_tx = create_pix(
        db=db,
        data=pix_request,
        idempotency_key="alice-to-bob-001",
        correlation_id="corr-001",
        user_id=user_alice.id,
        type=TransactionType.SENT
    )

    # Step 3: Validate sent transaction
    assert sent_tx.value == 300.00
    assert sent_tx.status == PixStatus.CONFIRMED
    assert sent_tx.user_id == user_alice.id
    assert sent_tx.type == TransactionType.SENT

    # Step 4: Validate balances updated
    db.refresh(user_alice)
    db.refresh(user_bob)

    assert user_alice.balance == 700.00  # 1000 - 300 (internal transfer: no fee)
    assert user_bob.balance == 300.00     # 0 + 300 (receiver pays no fee on internal)

    # Step 5: Validate received transaction was created
    recv_tx = db.query(PixTransaction).filter(
        PixTransaction.user_id == user_bob.id,
        PixTransaction.type == TransactionType.RECEIVED,
        PixTransaction.correlation_id == "corr-001"
    ).first()

    assert recv_tx is not None
    assert recv_tx.value == 300.00
    assert recv_tx.status == PixStatus.CONFIRMED


def test_internal_pix_transfer_by_email(db, user_alice, user_bob):
    """
    Tests internal PIX transfer using email key.
    """
    deposit_funds(db, user_alice.id, 500.00)

    pix_request = PixCreateRequest(
        value=150.00,
        pix_key="bob@biocodetechpay.com",
        key_type=PixKeyType.EMAIL,
        description="Email transfer test"
    )

    sent_tx = create_pix(
        db=db,
        data=pix_request,
        idempotency_key="alice-to-bob-email-001",
        correlation_id="corr-email-001",
        user_id=user_alice.id,
        type=TransactionType.SENT
    )

    db.refresh(user_alice)
    db.refresh(user_bob)

    assert user_alice.balance == 350.00  # 500 - 150 (internal transfer: no fee)
    assert user_bob.balance == 150.00
    assert sent_tx.status == PixStatus.CONFIRMED


def test_internal_pix_insufficient_balance(db, user_alice, user_bob):
    """
    Tests that internal PIX fails when sender has insufficient balance.
    """
    # Alice has only R$ 50
    deposit_funds(db, user_alice.id, 50.00)

    pix_request = PixCreateRequest(
        value=100.00,
        pix_key="22222222222",
        key_type=PixKeyType.CPF,
        description="Should fail"
    )

    with pytest.raises(ValueError, match="Saldo insuficiente"):
        create_pix(
            db=db,
            data=pix_request,
            idempotency_key="alice-fail-001",
            correlation_id="corr-fail-001",
            user_id=user_alice.id,
            type=TransactionType.SENT
        )

    # Balances should remain unchanged
    db.refresh(user_alice)
    db.refresh(user_bob)
    assert user_alice.balance == 50.00
    assert user_bob.balance == 0.00


def test_external_pix_creates_single_transaction(db, user_alice):
    """
    Tests that external PIX (key not found in BioCodeTechPay) creates only sender transaction.
    """
    deposit_funds(db, user_alice.id, 1000.00)

    pix_request = PixCreateRequest(
        value=200.00,
        pix_key="99999999999",  # External CPF not in BioCodeTechPay
        key_type=PixKeyType.CPF,
        description="External payment"
    )

    from unittest.mock import patch as _patch
    with _patch("app.pix.service.get_payment_gateway", return_value=None):
        sent_tx = create_pix(
            db=db,
            data=pix_request,
            idempotency_key="alice-external-001",
            correlation_id="corr-external-001",
            user_id=user_alice.id,
            type=TransactionType.SENT
        )

    assert sent_tx.status == PixStatus.CONFIRMED
    assert sent_tx.value == 200.00

    # Alice's balance should be updated: R$200 value + R$4 fee = R$204 total debited
    db.refresh(user_alice)
    assert user_alice.balance == pytest.approx(796.00, abs=0.01)  # 1000 - 200 - 4

    # No received transaction should exist (external)
    recv_count = db.query(PixTransaction).filter(
        PixTransaction.type == TransactionType.RECEIVED,
        PixTransaction.correlation_id == "corr-external-001"
    ).count()

    assert recv_count == 0


def test_multiple_internal_transfers(db, user_alice, user_bob):
    """
    Tests multiple sequential internal transfers.
    """
    deposit_funds(db, user_alice.id, 1000.00)
    deposit_funds(db, user_bob.id, 500.00)

    # Transfer 1: Alice -> Bob (R$ 200)
    create_pix(
        db=db,
        data=PixCreateRequest(value=200.00, pix_key="22222222222", key_type=PixKeyType.CPF),
        idempotency_key="tx-001",
        correlation_id="corr-001",
        user_id=user_alice.id,
        type=TransactionType.SENT
    )

    # Transfer 2: Bob -> Alice (R$ 150)
    create_pix(
        db=db,
        data=PixCreateRequest(value=150.00, pix_key="11111111111", key_type=PixKeyType.CPF),
        idempotency_key="tx-002",
        correlation_id="corr-002",
        user_id=user_bob.id,
        type=TransactionType.SENT
    )

    # Transfer 3: Alice -> Bob (R$ 100)
    create_pix(
        db=db,
        data=PixCreateRequest(value=100.00, pix_key="bob@biocodetechpay.com", key_type=PixKeyType.EMAIL),
        idempotency_key="tx-003",
        correlation_id="corr-003",
        user_id=user_alice.id,
        type=TransactionType.SENT
    )

    db.refresh(user_alice)
    db.refresh(user_bob)

    # Alice: 1000 - 200 + 150 - 100 = 850 (internal transfers: no fee)
    # Bob: 500 + 200 - 150 + 100 = 650
    assert user_alice.balance == 850.00
    assert user_bob.balance == 650.00
