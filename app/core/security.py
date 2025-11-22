"""
Cryptographic primitives and data masking implementation.
Enforces least privilege, auditability, and secure data handling standards.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from jose import jwt
from passlib.context import CryptContext
from app.core.config import settings

# Cryptographic context for password hashing (argon2)
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifies the provided plaintext password against the stored bcrypt hash.
    Uses constant-time comparison to prevent timing attacks.
    """
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """
    Generates a cryptographically strong bcrypt hash for the provided password.
    Work factor is automatically managed by passlib defaults.
    """
    return pwd_context.hash(password)


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """
    Generates a signed JWT (JSON Web Token) with configurable expiration.
    Enforces stateless authentication using HS256 algorithm.
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def mask_sensitive_data(value: str, mask_char: str = "*", visible_chars: int = 4) -> str:
    """
    Sanitizes sensitive information for audit logs, preserving only the trailing characters for identification purposes.
    """
    if not value or len(value) <= visible_chars:
        return mask_char * len(value) if value else ""

    return mask_char * (len(value) - visible_chars) + value[-visible_chars:]
