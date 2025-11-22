"""
Unit tests for PIX module.
Validates idempotency, status control, and validations.
"""
import pytest
from unittest.mock import Mock, MagicMock, patch
from app.pix.service import create_pix, confirm_pix
from app.pix.schemas import PixCreateRequest, PixKeyType
from app.pix.models import PixStatus


def test_create_pix_success():
    """Tests successful PIX creation."""
    db_mock = MagicMock()
    db_mock.query().filter().first.return_value = None  # No duplicate PIX

    data = PixCreateRequest(
        value=150.0,
        pix_key="teste@email.com",
        key_type=PixKeyType.EMAIL,
        description="Pagamento teste"
    )

    # Mock get_balance to return sufficient balance
    with patch("app.pix.service.get_balance", return_value=1000.0):
        pix = create_pix(db_mock, data, "idem-key-123", "corr-123", "user-123")

    assert pix.value == 150.0
    assert pix.status == PixStatus.CREATED
    assert pix.idempotency_key == "idem-key-123"


def test_pix_idempotency():
    """Tests that idempotency returns existing PIX."""
    existing_pix = Mock()
    existing_pix.id = "pix-123"
    existing_pix.value = 200.0

    db_mock = MagicMock()
    db_mock.query().filter().first.return_value = existing_pix

    data = PixCreateRequest(
        value=200.0,
        pix_key="chave@test.com",
        key_type=PixKeyType.EMAIL,
        description="Teste idempotência"
    )

    pix = create_pix(db_mock, data, "idem-key-duplicate", "corr-123", "user-123")

    assert pix.id == "pix-123"
    assert pix.value == 200.0


def test_cpf_validation():
    """Validates CPF format."""
    with pytest.raises(Exception):
        PixCreateRequest(
            value=100.0,
            pix_key="12345",  # Invalid CPF
            key_type=PixKeyType.CPF,
            description="Teste CPF inválido"
        )


def test_email_validation():
    """Validates email format."""
    with pytest.raises(Exception):
        PixCreateRequest(
            value=100.0,
            pix_key="email-invalido",
            key_type=PixKeyType.EMAIL,
            description="Teste email inválido"
        )


def test_negative_value_validation():
    """Validates rejection of negative value."""
    with pytest.raises(Exception):
        PixCreateRequest(
            value=-50.0,
            pix_key="teste@email.com",
            key_type=PixKeyType.EMAIL,
            description="Teste valor negativo"
        )


def test_pix_confirmation():
    """Tests PIX confirmation."""
    pix_mock = Mock()
    pix_mock.id = "pix-456"
    pix_mock.status = PixStatus.CREATED

    db_mock = MagicMock()
    db_mock.query().filter().first.return_value = pix_mock

    pix = confirm_pix(db_mock, "pix-456", "corr-123")

    assert pix is not None
    assert pix.status == PixStatus.CONFIRMED


def test_confirm_non_existent_pix():
    """Tests confirmation of non-existent PIX."""
    db_mock = MagicMock()
    db_mock.query().filter().first.return_value = None

    pix = confirm_pix(db_mock, "pix-non-existent", "corr-123")

    assert pix is None


def test_phone_validation():
    """Validates phone format."""
    # Valid phone with 11 digits
    pix = PixCreateRequest(
        value=100.0,
        pix_key="11987654321",
        key_type=PixKeyType.PHONE,
        description="Teste telefone válido"
    )
    assert pix.pix_key == "11987654321"

    # Invalid phone
    with pytest.raises(Exception):
        PixCreateRequest(
            value=100.0,
            pix_key="123",
            key_type=PixKeyType.PHONE,
            description="Teste telefone inválido"
        )
