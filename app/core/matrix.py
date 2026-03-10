"""
Matrix account management — Bio Code Technology fee-collection system account.

All service fees (external PIX, Boleto) are credited here automatically.
The admin can then initiate PIX transfers from this balance to external keys.
"""
import secrets
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.logger import logger


def get_matrix_user(db: Session):
    """Returns the matrix system account User or None if not yet seeded."""
    from app.auth.models import User
    return db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()


def credit_fee(db: Session, amount: float) -> None:
    """
    Credits a fee amount to the matrix account balance.
    Must be called within an active DB transaction — the caller is responsible for commit.
    Silently skips if amount <= 0 or matrix account is missing (non-blocking by design).
    """
    if amount <= 0:
        return
    matrix = get_matrix_user(db)
    if not matrix:
        logger.warning("Matrix account not found — fee credit skipped. Run seed_matrix_account() on startup.")
        return
    matrix.balance += amount
    db.add(matrix)


def seed_matrix_account() -> None:
    """
    Ensures the Bio Code Technology matrix account exists in the database.
    Idempotent: safe to call on every application startup.
    Creates the account with a random non-guessable password (account is never logged into directly).
    """
    from app.auth.models import User
    from app.auth.service import get_password_hash
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        existing = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
        if existing:
            return

        matrix_user = User(
            name=settings.MATRIX_ACCOUNT_NAME,
            cpf_cnpj=settings.MATRIX_ACCOUNT_CNPJ,
            email=settings.MATRIX_ACCOUNT_EMAIL,
            hashed_password=get_password_hash(secrets.token_hex(32)),
            balance=0.0,
            is_admin=False,
            is_active=True,
            email_verified=True,
            document_verified=True,
        )
        db.add(matrix_user)
        db.commit()
        logger.info(
            "Matrix account seeded: Bio Code Technology fee-collection account created",
            extra={"email": settings.MATRIX_ACCOUNT_EMAIL},
        )
