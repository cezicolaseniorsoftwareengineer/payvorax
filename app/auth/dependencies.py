from fastapi import Request, Response, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.database import get_db
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from datetime import timedelta

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def _decode_access_token(raw_cookie: str) -> dict | None:
    """Returns payload dict if valid access token; None on any error."""
    try:
        scheme, _, param = raw_cookie.partition(" ")
        token = param if param else scheme
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        # Reject refresh tokens used as access tokens
        if payload.get("type") == "refresh":
            return None
        return payload
    except JWTError:
        return None


def _try_refresh(refresh_raw: str, response: Response, db: Session) -> "User | None":
    """
    Validates the refresh token, issues new access + refresh tokens via response cookies,
    and returns the user. Returns None if the refresh token is invalid or expired.
    """
    from app.auth.service import create_access_token, create_refresh_token
    try:
        payload = jwt.decode(refresh_raw, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "refresh":
            return None
        cpf_cnpj = payload.get("sub")
        if not cpf_cnpj:
            return None
    except JWTError:
        return None

    user = db.query(User).filter(User.cpf_cnpj == cpf_cnpj).first()
    if not user or not user.email_verified or not user.is_active:
        return None

    new_access = create_access_token(
        data={"sub": user.cpf_cnpj, "name": user.name},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    new_refresh = create_refresh_token(data={"sub": user.cpf_cnpj, "name": user.name})

    response.set_cookie(
        key="access_token",
        value=f"Bearer {new_access}",
        httponly=True, secure=True, samesite="strict",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        expires=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=new_refresh,
        httponly=True, secure=True, samesite="strict",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
        expires=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
    )
    return user


def get_current_user(request: Request, response: Response, db: Session = Depends(get_db)):
    """
    Extracts the current user from the access_token cookie.
    On access token expiry, transparently attempts refresh via refresh_token cookie.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    access_token_raw = request.cookies.get("access_token")
    refresh_token_raw = request.cookies.get("refresh_token")

    user = None

    # 1. Try access token
    if access_token_raw:
        payload = _decode_access_token(access_token_raw)
        if payload:
            cpf_cnpj = payload.get("sub")
            if cpf_cnpj and isinstance(cpf_cnpj, str):
                user = db.query(User).filter(User.cpf_cnpj == cpf_cnpj).first()

    # 2. Access token invalid/expired — try refresh
    if not user and refresh_token_raw:
        user = _try_refresh(refresh_token_raw, response, db)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    # Defense in depth: even with a valid JWT, block access if email was never
    # verified. This covers tokens issued before this enforcement was added.
    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="E-mail nao verificado. Confirme seu e-mail antes de acessar."
        )

    return user


def require_active_account(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> User:
    """
    Verifies if the user has made at least one deposit (Incoming PIX).
    Blocks access to critical features if the account is not active.
    """
    has_deposit = db.query(PixTransaction).filter(
        PixTransaction.user_id == user.id,
        PixTransaction.type == TransactionType.RECEIVED,
        PixTransaction.status == PixStatus.CONFIRMED
    ).first()

    if not has_deposit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive account. Make a first deposit (Received PIX) to unlock all features."
        )

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """
    Admin gate — uses the is_admin boolean flag from the database.
    Never relies on email comparison for authorization decisions.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Acesso restrito.")
    return user
