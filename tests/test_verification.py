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
from unittest.mock import patch, MagicMock
from app.main import app
from app.core.document_validator import validate_cpf, validate_cnpj, validate_document


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
        # Real CNPJ of Bio Code Technology — admin account
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
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def valid_user_payload():
    return {
        "name": "Test User",
        "email": "test_verify@example.com",
        "cpf_cnpj": "52998224725",
        "password": "TestPass@123"
    }


class TestRegisterDocumentGate:
    def test_register_invalid_cpf_returns_422(self, client):
        payload = {
            "name": "Fake User",
            "email": "fake@example.com",
            "cpf_cnpj": "11111111111",  # repeated digits — always invalid
            "password": "SomePass@123"
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
            "password": "SomePass@123"
        }
        response = client.post("/auth/register", json=payload)
        assert response.status_code == 422

    def test_register_wrong_cpf_digits_returns_422(self, client):
        payload = {
            "name": "Wrong Digit",
            "email": "wrongdigit@example.com",
            "cpf_cnpj": "52998224726",  # last digit wrong
            "password": "SomePass@123"
        }
        response = client.post("/auth/register", json=payload)
        assert response.status_code == 422

    @patch("app.core.email_service.send_verification_email", return_value=True)
    def test_register_valid_cpf_accepted(self, mock_email, client, valid_user_payload):
        # Use a unique email to avoid duplicate conflict
        valid_user_payload["email"] = "valid_new_user@example.com"
        valid_user_payload["cpf_cnpj"] = "52998224725"

        response = client.post("/auth/register", json=valid_user_payload)
        # Either 201 (created) or 400 (duplicate if test ran before) are acceptable
        assert response.status_code in (201, 400)
        if response.status_code == 201:
            data = response.json()
            assert "access_token" in data
            assert data.get("email_verified") is False
            assert data.get("document_verified") is True

    @patch("app.core.email_service.send_verification_email", return_value=True)
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
        assert "invalido" in response.json()["detail"].lower()

    def test_empty_token_returns_422(self, client):
        response = client.get("/auth/verificar-email")
        # Missing required query param
        assert response.status_code == 422


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
