"""
Tests: silent refresh token rotation in get_current_user.

Covers:
- Valid access token grants access normally
- Expired access token is rejected (no refresh cookie) with 401
- Expired access token + valid refresh cookie grants silent rotation:
    - New access_token cookie set in response
    - New refresh_token cookie set in response
    - User object returned correctly
- Refresh token with wrong type claim is rejected
- Refresh token for inactive / unverified user is rejected
"""

import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock
from jose import jwt
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.config import settings
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.auth.service import create_access_token, create_refresh_token, get_password_hash

# ---------------------------------------------------------------------------
# In-memory SQLite DB
# ---------------------------------------------------------------------------

_TEST_URL = "sqlite:///:memory:"
_engine = create_engine(
    _TEST_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _db_override():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Minimal FastAPI app for dependency testing
# ---------------------------------------------------------------------------

_test_app = FastAPI()


@_test_app.get("/protected")
def _protected_endpoint(current_user=Depends(get_current_user)):
    return {"user_id": current_user.id, "cpf_cnpj": current_user.cpf_cnpj}


_test_app.dependency_overrides[get_db] = _db_override


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def db():
    session = _Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def active_user(db):
    """Creates a verified active user for auth tests."""
    user = User(
        name="Auth Test User",
        email="authtest@example.com",
        cpf_cnpj="11144477735",
        hashed_password=get_password_hash("test-password"),
        email_verified=True,
        is_active=True,
        balance=Decimal("0.00"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def client():
    return TestClient(_test_app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_access_cookie(cpf_cnpj: str, expire_delta: timedelta) -> str:
    token = create_access_token(
        data={"sub": cpf_cnpj, "name": "Auth Test User"},
        expires_delta=expire_delta,
    )
    return f"Bearer {token}"


def _make_refresh_cookie(cpf_cnpj: str) -> str:
    return create_refresh_token(data={"sub": cpf_cnpj, "name": "Auth Test User"})


def _make_expired_cookie(cpf_cnpj: str) -> str:
    """Issues an access token that expired 10 minutes ago."""
    return _make_access_cookie(cpf_cnpj, timedelta(minutes=-10))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAccessTokenValidation:
    def test_valid_access_token_grants_access(self, client, active_user):
        cookie = _make_access_cookie(active_user.cpf_cnpj, timedelta(minutes=15))
        resp = client.get("/protected", cookies={"access_token": cookie})
        assert resp.status_code == 200
        assert resp.json()["cpf_cnpj"] == active_user.cpf_cnpj

    def test_no_cookies_returns_401(self, client):
        resp = client.get("/protected")
        assert resp.status_code == 401

    def test_expired_access_no_refresh_returns_401(self, client, active_user):
        cookie = _make_expired_cookie(active_user.cpf_cnpj)
        resp = client.get("/protected", cookies={"access_token": cookie})
        assert resp.status_code == 401

    def test_tampered_access_token_returns_401(self, client, active_user):
        resp = client.get("/protected", cookies={"access_token": "Bearer invalid.token.here"})
        assert resp.status_code == 401

    def test_refresh_token_rejected_as_access_token(self, client, active_user):
        """A refresh token must NOT be accepted where an access token is expected."""
        refresh = _make_refresh_cookie(active_user.cpf_cnpj)
        resp = client.get("/protected", cookies={"access_token": f"Bearer {refresh}"})
        # Should fall through to 401 (refresh token type check blocks it)
        assert resp.status_code == 401


class TestSilentRefreshRotation:
    def test_expired_access_valid_refresh_grants_access(self, client, active_user):
        """
        Core invariant: expired access + valid refresh => 200 + new cookies set.
        The response must also re-issue both cookies.
        """
        expired_access = _make_expired_cookie(active_user.cpf_cnpj)
        valid_refresh = _make_refresh_cookie(active_user.cpf_cnpj)

        resp = client.get(
            "/protected",
            cookies={
                "access_token": expired_access,
                "refresh_token": valid_refresh,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["cpf_cnpj"] == active_user.cpf_cnpj

        # New cookies must be issued (Set-Cookie headers present in response)
        assert "access_token" in resp.cookies, "New access_token cookie not issued on silent refresh"
        assert "refresh_token" in resp.cookies, "New refresh_token cookie not issued on silent refresh"

    def test_only_refresh_cookie_grants_access(self, client, active_user):
        """No access token at all, but valid refresh cookie => 200."""
        valid_refresh = _make_refresh_cookie(active_user.cpf_cnpj)

        resp = client.get("/protected", cookies={"refresh_token": valid_refresh})
        assert resp.status_code == 200

    def test_expired_refresh_token_returns_401(self, client, active_user):
        to_encode = {
            "sub": active_user.cpf_cnpj,
            "name": "Auth Test User",
            "type": "refresh",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=10),
        }
        expired_refresh = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        expired_access = _make_expired_cookie(active_user.cpf_cnpj)

        resp = client.get(
            "/protected",
            cookies={
                "access_token": expired_access,
                "refresh_token": expired_refresh,
            },
        )
        assert resp.status_code == 401

    def test_inactive_user_refresh_returns_401(self, db, client, active_user):
        """Suspended user must not get silent refresh."""
        active_user.is_active = False
        db.commit()

        expired_access = _make_expired_cookie(active_user.cpf_cnpj)
        valid_refresh = _make_refresh_cookie(active_user.cpf_cnpj)

        resp = client.get(
            "/protected",
            cookies={
                "access_token": expired_access,
                "refresh_token": valid_refresh,
            },
        )
        assert resp.status_code == 401

    def test_unverified_email_refresh_returns_401(self, db, client, active_user):
        """Unverified email must not be silently refreshed."""
        active_user.email_verified = False
        db.commit()

        expired_access = _make_expired_cookie(active_user.cpf_cnpj)
        valid_refresh = _make_refresh_cookie(active_user.cpf_cnpj)

        resp = client.get(
            "/protected",
            cookies={
                "access_token": expired_access,
                "refresh_token": valid_refresh,
            },
        )
        assert resp.status_code == 401

    def test_refresh_wrong_type_claim_returns_401(self, client, active_user):
        """Token with missing 'type=refresh' claim must not be accepted as refresh."""
        impostor = create_access_token(
            data={"sub": active_user.cpf_cnpj, "name": "Auth Test User"},
            expires_delta=timedelta(days=7),
        )
        expired_access = _make_expired_cookie(active_user.cpf_cnpj)

        resp = client.get(
            "/protected",
            cookies={
                "access_token": expired_access,
                "refresh_token": impostor,
            },
        )
        assert resp.status_code == 401
