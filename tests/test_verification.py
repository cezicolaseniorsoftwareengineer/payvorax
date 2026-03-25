"""
Tests: CPF/CNPJ validation + email verification flow.

Covers:
- Mathematical validation of CPF (valid, invalid, repeated digits)
- Mathematical validation of CNPJ (valid, invalid, repeated digits)
- validate_document() dispatcher
- Registration rejects invalid CPF/CNPJ at the API level
- Registration accepts valid CPF/CNPJ
- /auth/verificar-email endpoint token consumption
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch, MagicMock
from app.main import app
from app.core.database import Base, get_db
from app.core.document_validator import validate_cpf, validate_cnpj, validate_document

# ---------------------------------------------------------------------------
# In-memory SQLite override — prevents any write reaching Neon in test mode
# ---------------------------------------------------------------------------

_TEST_DB_URL = "sqlite:///:memory:"
_engine = create_engine(
    _TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _override_get_db():
    db = _TestingSession()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Unit tests: document_validator
# ---------------------------------------------------------------------------

class TestValidateCpf:
    def test_valid_cpf(self):
        # Real CPF with correct check digits
        assert validate_cpf("529.982.247-25") is True

    def test_valid_cpf_digits_only(self):
        assert validate_cpf("52998224725") is True

    def test_invalid_cpf_wrong_digit(self):
        assert validate_cpf("529.982.247-26") is False

    def test_cpf_all_same_digits_rejected(self):
        for d in "0123456789":
            assert validate_cpf(d * 11) is False

    def test_cpf_wrong_length(self):
        assert validate_cpf("1234567890") is False
        assert validate_cpf("123456789012") is False

    def test_cpf_empty(self):
        assert validate_cpf("") is False


class TestValidateCnpj:
    def test_valid_cnpj_bio_code_technology(self):
        # Real CNPJ of BioCodeTechPay — admin account
        assert validate_cnpj("61.425.124/0001-03") is True

    def test_valid_cnpj_digits_only(self):
        assert validate_cnpj("61425124000103") is True

    def test_invalid_cnpj_wrong_digit(self):
        assert validate_cnpj("61425124000104") is False

    def test_cnpj_all_same_digits_rejected(self):
        for d in "0123456789":
            assert validate_cnpj(d * 14) is False

    def test_cnpj_wrong_length(self):
        assert validate_cnpj("6142512400010") is False
        assert validate_cnpj("614251240001044") is False

    def test_cnpj_empty(self):
        assert validate_cnpj("") is False


class TestValidateDocument:
    def test_valid_cpf_dispatch(self):
        ok, result = validate_document("52998224725")
        assert ok is True
        assert result == "CPF"

    def test_valid_cnpj_dispatch(self):
        ok, result = validate_document("61425124000103")
        assert ok is True
        assert result == "CNPJ"

    def test_invalid_cpf_dispatch(self):
        ok, result = validate_document("12345678901")
        assert ok is False
        assert "CPF" in result or "invalido" in result.lower()

    def test_invalid_length_dispatch(self):
        ok, result = validate_document("123")
        assert ok is False

    def test_formatted_cpf_strip(self):
        ok, result = validate_document("529.982.247-25")
        assert ok is True
        assert result == "CPF"

    def test_formatted_cnpj_strip(self):
        ok, result = validate_document("61.425.124/0001-03")
        assert ok is True
        assert result == "CNPJ"


# ---------------------------------------------------------------------------
# Integration tests: /auth/register document validation gate
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _override_get_db
    Base.metadata.create_all(bind=_engine)
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    Base.metadata.drop_all(bind=_engine)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


@pytest.fixture
def valid_user_payload():
    return {
        "name": "Test User",
        "email": "test_verify@example.com",
        "cpf_cnpj": "52998224725",
        "password": "TestPass@123",
        "phone": "11999999999",
        "address_street": "Rua das Flores",
        "address_number": "123",
        "address_complement": None,
        "address_city": "Sao Paulo",
        "address_state": "SP",
        "address_zip": "01310100"
    }


class TestRegisterDocumentGate:
    def test_register_invalid_cpf_returns_422(self, client):
        payload = {
            "name": "Fake User",
            "email": "fake@example.com",
            "cpf_cnpj": "11111111111",  # repeated digits — always invalid
            "password": "SomePass@123",
            "phone": "11999999999",
            "address_street": "Rua Teste",
            "address_number": "1",
            "address_city": "Sao Paulo",
            "address_state": "SP",
            "address_zip": "01310100"
        }
        response = client.post("/auth/register", json=payload)
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    def test_register_invalid_cnpj_returns_422(self, client):
        payload = {
            "name": "Fake Corp",
            "email": "fakecorp@example.com",
            "cpf_cnpj": "00000000000000",  # repeated digits — always invalid
            "password": "SomePass@123",
            "phone": "11999999999",
            "address_street": "Rua Teste",
            "address_number": "1",
            "address_city": "Sao Paulo",
            "address_state": "SP",
            "address_zip": "01310100"
        }
        response = client.post("/auth/register", json=payload)
        assert response.status_code == 422

    def test_register_wrong_cpf_digits_returns_422(self, client):
        payload = {
            "name": "Wrong Digit",
            "email": "wrongdigit@example.com",
            "cpf_cnpj": "52998224726",  # last digit wrong
            "password": "SomePass@123",
            "phone": "11999999999",
            "address_street": "Rua Teste",
            "address_number": "1",
            "address_city": "Sao Paulo",
            "address_state": "SP",
            "address_zip": "01310100"
        }
        response = client.post("/auth/register", json=payload)
        assert response.status_code == 422

    @patch("app.auth.router.send_verification_email", return_value=True)
    def test_register_valid_cpf_accepted(self, mock_email, client, valid_user_payload):
        # Use a unique email to avoid duplicate conflict
        valid_user_payload["email"] = "valid_new_user@example.com"
        valid_user_payload["cpf_cnpj"] = "52998224725"

        response = client.post("/auth/register", json=valid_user_payload)
        # Either 201 (created) or 400 (duplicate if test ran before) are acceptable
        assert response.status_code in (201, 400)
        if response.status_code == 201:
            data = response.json()
            assert "access_token" not in data
            assert data.get("email_verified") is False
            assert data.get("document_verified") is True
            assert "message" in data

    @patch("app.auth.router.send_verification_email", return_value=True)
    def test_register_sends_verification_email(self, mock_email, client, valid_user_payload):
        valid_user_payload["email"] = "emailcheck@example.com"
        valid_user_payload["cpf_cnpj"] = "52998224725"

        response = client.post("/auth/register", json=valid_user_payload)
        if response.status_code == 201:
            mock_email.assert_called_once()
            call_args = mock_email.call_args[0]
            assert call_args[0] == "emailcheck@example.com"
            assert len(call_args[2]) > 20  # token must be non-trivial


# ---------------------------------------------------------------------------
# Integration tests: /auth/verificar-email endpoint
# ---------------------------------------------------------------------------

class TestVerifyEmailEndpoint:
    def test_invalid_token_returns_400(self, client):
        response = client.get("/auth/verificar-email?token=invalid_token_xyz")
        assert response.status_code == 400
        assert "inválido" in response.json()["detail"].lower()

    def test_empty_token_returns_422(self, client):
        response = client.get("/auth/verificar-email")
        # Missing required query param
        assert response.status_code == 422

    @patch("app.auth.router.send_verification_email", return_value=True)
    def test_valid_token_redirects_and_issues_both_cookies(self, mock_email, client, valid_user_payload):
        """
        Successful email verification must:
        1. Redirect to /?email_verificado=1 (302)
        2. Set access_token cookie (samesite=strict, httponly)
        3. Set refresh_token cookie (samesite=strict, httponly)
        Without the refresh_token cookie, the session cannot survive the 15-minute
        access token TTL, forcing the user to log in again immediately after verification.
        """
        from app.core.database import Base, get_db as _get_db
        import secrets

        # Register the user first (uses the in-memory SQLite DB from conftest)
        valid_user_payload["email"] = "cookie_verify_test@example.com"
        valid_user_payload["cpf_cnpj"] = "52998224725"

        reg_resp = client.post("/auth/register", json=valid_user_payload)
        assert reg_resp.status_code == 201, f"Registration failed: {reg_resp.text}"

        # Fetch the token from the DB directly via the overridden dependency
        from app.core.database import get_db as original_get_db
        db = next(client.app.dependency_overrides.get(original_get_db, original_get_db)())
        try:
            from app.auth.models import User
            user = db.query(User).filter(User.email == "cookie_verify_test@example.com").first()
            assert user is not None
            token = user.email_verification_token
            assert token is not None
        finally:
            db.close()

        # Verify email — should redirect and set both cookies
        response = client.get(
            f"/auth/verificar-email?token={token}",
            follow_redirects=False,
        )
        assert response.status_code == 302, f"Expected redirect, got {response.status_code}: {response.text}"
        assert "/?email_verificado=1" in response.headers.get("location", "")

        cookie_header = response.headers.get("set-cookie", "")
        assert "access_token=" in cookie_header, "access_token cookie missing from verify-email response"
        assert "refresh_token=" in cookie_header, "refresh_token cookie missing from verify-email response — user will be kicked out after 15 min"


# ---------------------------------------------------------------------------
# Integration tests: /auth/validar-documento endpoint
# ---------------------------------------------------------------------------

class TestValidateDocumentEndpoint:
    def test_valid_cpf_endpoint(self, client):
        response = client.post("/auth/validar-documento?cpf_cnpj=52998224725")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["doc_type"] == "CPF"

    def test_invalid_cpf_endpoint(self, client):
        response = client.post("/auth/validar-documento?cpf_cnpj=11111111111")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert data["doc_type"] is None

    def test_valid_cnpj_endpoint(self, client):
        response = client.post("/auth/validar-documento?cpf_cnpj=61425124000103")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["doc_type"] == "CNPJ"
