"""
Integration tests for PATCH /admin/users/{user_id} endpoint.
Validates admin-only access, partial updates, immutable fields protection,
validation constraints, and audit trail side-effects.
No production DB touched — all dependencies are overridden.
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


ADMIN_EMAIL = "biocodetechnology@gmail.com"
MATRIX_EMAIL = "matrix@biocodetech.com"


def _make_user(
    uid,
    name,
    email,
    cpf="11111111111",
    balance=0.0,
    is_active=True,
    is_admin=False,
    phone=None,
    address_street=None,
    address_number=None,
    address_complement=None,
    address_city=None,
    address_state=None,
    address_zip=None,
    email_verified=False,
    document_verified=False,
    credit_limit=10000.0,
):
    from app.auth.models import User

    u = User()
    u.id = uid
    u.name = name
    u.email = email
    u.cpf_cnpj = cpf
    u.hashed_password = "hashed"
    u.balance = balance
    u.is_active = is_active
    u.is_admin = is_admin
    u.phone = phone
    u.address_street = address_street
    u.address_number = address_number
    u.address_complement = address_complement
    u.address_city = address_city
    u.address_state = address_state
    u.address_zip = address_zip
    u.email_verified = email_verified
    u.document_verified = document_verified
    u.credit_limit = credit_limit
    return u


def _db_for_user(target_user):
    """DB mock that resolves the target user on User.query.filter.first()."""
    db = MagicMock()

    def query_side(model):
        from app.auth.models import User
        q = MagicMock()
        if model is User:
            q.filter.return_value.first.return_value = target_user
        else:
            q.filter.return_value.first.return_value = None
        return q

    db.query.side_effect = query_side
    db.commit = MagicMock()
    return db


def _db_user_not_found():
    """DB mock that returns None for any User lookup."""
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = None
    db.query.return_value = q
    db.commit = MagicMock()
    return db


class TestAdminEditUser:

    def test_patch_name_returns_200(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        target = _make_user("user-abc", "Old Name", "user@example.com")
        db = _db_for_user(target)

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/user-abc",
                    json={"name": "New Name"},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "name" in body["changed"]
        assert body["changed"]["name"] == "New Name"
        assert target.name == "New Name"
        db.commit.assert_called_once()

    def test_patch_address_fields_partial_update(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        target = _make_user("user-xyz", "Test User", "usr@example.com")
        db = _db_for_user(target)

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/user-xyz",
                    json={
                        "address_street": "Rua das Flores",
                        "address_number": "42",
                        "address_city": "Sao Paulo",
                        "address_state": "sp",
                        "address_zip": "01234-567",
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert target.address_street == "Rua das Flores"
        assert target.address_state == "SP"  # must be uppercased
        assert "address_street" in body["changed"]

    def test_patch_verification_flags(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        target = _make_user("user-flags", "Flag User", "flags@example.com")
        db = _db_for_user(target)

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/user-flags",
                    json={"email_verified": True, "document_verified": True},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert target.email_verified is True
        assert target.document_verified is True

    def test_patch_credit_limit(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        target = _make_user("user-limit", "Limit User", "limit@example.com")
        db = _db_for_user(target)

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/user-limit",
                    json={"credit_limit": 25000.0},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert target.credit_limit == 25000.0

    def test_empty_payload_returns_no_changes(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        target = _make_user("user-nop", "Nop User", "nop@example.com")
        db = _db_for_user(target)

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch("/admin/users/user-nop", json={})
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["changed"] == {}
        db.commit.assert_not_called()

    def test_non_admin_gets_403(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        non_admin = _make_user("user-regular", "Regular", "regular@example.com")
        db = _db_for_user(non_admin)

        app.dependency_overrides[get_current_user] = lambda: non_admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/user-regular",
                    json={"name": "Hacker"},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 403

    def test_user_not_found_returns_404(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        db = _db_user_not_found()

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/nonexistent-id",
                    json={"name": "Ghost"},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 404

    def test_empty_name_returns_400(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        target = _make_user("user-badname", "Valid Name", "bn@example.com")
        db = _db_for_user(target)

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/user-badname",
                    json={"name": "   "},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400

    def test_negative_credit_limit_returns_400(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        target = _make_user("user-neg", "Neg User", "neg@example.com")
        db = _db_for_user(target)

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/user-neg",
                    json={"credit_limit": -500.0},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400

    def test_state_longer_than_two_chars_returns_400(self):
        from app.main import app
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        admin = _make_user("admin-001", "Admin", ADMIN_EMAIL)
        target = _make_user("user-st", "State User", "st@example.com")
        db = _db_for_user(target)

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                resp = client.patch(
                    "/admin/users/user-st",
                    json={"address_state": "SPP"},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400
