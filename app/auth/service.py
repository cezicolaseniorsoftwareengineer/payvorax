from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from jose import jwt
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from app.core.config import settings
from app.auth.models import User
from app.core.logger import audit_log, logger

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: Dict[str, Any]) -> str:
    """Issues a long-lived refresh token (type=refresh). Never grants resource access directly."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# Canonical implementations live in app.pix.service (financial domain).
# Re-exported here to preserve the existing import surface in auth.router.
from app.pix.service import get_user_balance, deposit_funds  # noqa: F401
