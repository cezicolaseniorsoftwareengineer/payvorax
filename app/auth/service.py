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


def get_user_balance(db: Session, user_id: str) -> float:
    """
    Returns the current balance from the user account.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"User {user_id} not found")
    return user.balance


def deposit_funds(
    db: Session,
    user_id: str,
    amount: float,
    description: str = "Deposit",
    correlation_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Deposits funds into user account.
    Simulates receiving money in the BioCodeTechPay internal bank.

    Args:
        db: Database session
        user_id: Target user ID
        amount: Amount to deposit (must be positive)
        description: Deposit description for audit
        correlation_id: Optional correlation ID for tracing

    Returns:
        Dict with new balance and transaction details

    Raises:
        ValueError: If amount is invalid or user not found
    """
    if amount <= 0:
        raise ValueError("Deposit amount must be positive")

    if amount > 1000000:
        raise ValueError("Deposit amount exceeds limit of R$ 1,000,000.00")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"User {user_id} not found")

    previous_balance = user.balance
    user.balance = Decimal(str(user.balance)) + Decimal(str(amount))
    db.add(user)
    db.commit()
    db.refresh(user)

    audit_log(
        action="deposit_funds",
        user=user_id,
        resource=f"user_id={user_id}",
        details={
            "amount": amount,
            "previous_balance": previous_balance,
            "new_balance": user.balance,
            "description": description,
            "correlation_id": correlation_id
        }
    )

    logger.info(
        f"Deposit successful: user={user_id}, amount={amount:.2f}, "
        f"previous_balance={previous_balance:.2f}, new_balance={user.balance:.2f}"
    )

    return {
        "user_id": user_id,
        "amount": amount,
        "previous_balance": previous_balance,
        "new_balance": user.balance,
        "description": description,
        "timestamp": datetime.now(timezone.utc)
    }
