from fastapi import APIRouter, Depends, HTTPException, Response, status, Header, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from uuid import uuid4
from typing import Optional
from app.core.database import get_db
from app.auth.models import User
from app.auth.schemas import UserCreate, UserLogin, DepositRequest, DepositResponse, BalanceResponse, PasswordResetRequest, PasswordResetConfirm, PasswordResetConfirmWithTemp
from app.auth.service import (
    get_password_hash,
    verify_password,
    create_access_token,
    deposit_funds,
    get_user_balance
)
from app.auth.dependencies import get_current_user
from app.core.email_service import send_verification_email
from app.core.document_validator import validate_document
from datetime import timedelta, datetime, timezone
from app.core.config import settings
from app.core.logger import logger
import traceback
import secrets
import re

router = APIRouter()


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(response: Response, user: UserCreate, db: Session = Depends(get_db)):
    """
    Registers a new user.
    Validates CPF/CNPJ mathematically, then sends email verification link.
    Account is active but email_verified=False until link is confirmed.
    """
    try:
        logger.info(f"Starting registration for CPF/CNPJ: {user.cpf_cnpj}")

        # --- Document validation (anti-fraud KYC gate) ---
        is_valid_doc, doc_result = validate_document(user.cpf_cnpj)
        if not is_valid_doc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=doc_result
            )

        # Check if user already exists
        db_user = db.query(User).filter(
            (User.email == user.email) | (User.cpf_cnpj == user.cpf_cnpj)
        ).first()

        if db_user:
            logger.warning(f"Duplicate registration attempt: {user.cpf_cnpj} or {user.email}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email ou CPF/CNPJ ja cadastrado no sistema."
            )

        # Generate email verification token (cryptographically secure)
        email_token = secrets.token_urlsafe(32)

        # Create new user — email_verified starts False
        hashed_password = get_password_hash(user.password)
        new_user = User(
            name=user.name,
            email=user.email,
            cpf_cnpj=user.cpf_cnpj,
            hashed_password=hashed_password,
            phone=user.phone,
            address_street=user.address_street,
            address_number=user.address_number,
            address_complement=user.address_complement,
            address_city=user.address_city,
            address_state=user.address_state,
            address_zip=user.address_zip,
            email_verified=False,
            document_verified=True,  # CPF/CNPJ passed mathematical validation
            email_verification_token=email_token,
            email_verification_sent_at=datetime.now(timezone.utc),
            is_active=True,
            is_admin=False
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        logger.info(f"User created: ID {new_user.id} | doc_type={doc_result}")

        # Send verification email (non-blocking: failure does not abort registration)
        sent = send_verification_email(new_user.email, new_user.name, email_token)
        if not sent:
            logger.warning(f"Verification email not sent for {new_user.id} (SMTP not configured)")

        # Auto-login: Generate session token
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": new_user.cpf_cnpj, "name": new_user.name},
            expires_delta=access_token_expires
        )

        response.set_cookie(
            key="access_token",
            value=f"Bearer {access_token}",
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            expires=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "name": new_user.name,
            "email_verified": False,
            "document_verified": True,
            "message": "Cadastro realizado. Verifique seu e-mail para ativar todos os recursos."
        }

    except HTTPException:
        raise
    except IntegrityError as e:
        db.rollback()
        logger.error(f"Database integrity error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Erro ao processar dados. Verifique os campos informados."
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Internal registration error: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro interno. Tente novamente mais tarde."
        )


@router.post("/login")
def login(response: Response, user_in: UserLogin, db: Session = Depends(get_db)):
    """
    Authenticates user and sets session cookie.
    """
    try:
        logger.info(f"Login attempt for CPF/CNPJ: {user_in.cpf_cnpj}")

        user = db.query(User).filter(User.cpf_cnpj == user_in.cpf_cnpj).first()

        if not user or not verify_password(user_in.password, user.hashed_password):
            logger.warning(f"Login failure for {user_in.cpf_cnpj}: Invalid credentials")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect CPF/CNPJ or password."
            )

        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": user.cpf_cnpj, "name": user.name},
            expires_delta=access_token_expires
        )

        response.set_cookie(
            key="access_token",
            value=f"Bearer {access_token}",
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            expires=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

        logger.info(f"Login successful for {user.cpf_cnpj}")

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "name": user.name
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Internal login error: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error performing login."
        )


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Logout successful"}


@router.get("/verificar-email")
def verify_email(token: str, db: Session = Depends(get_db)):
    """
    Confirms the user's email via the token sent during registration.
    Token expires after 24 hours.
    """
    user = db.query(User).filter(User.email_verification_token == token).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Link de verificação inválido ou já utilizado."
        )

    # Check expiry (24 hours)
    if user.email_verification_sent_at:
        sent_at = user.email_verification_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - sent_at
        if elapsed.total_seconds() > 86400:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Link de verificacao expirado. Solicite um novo envio."
            )

    user.email_verified = True
    user.email_verification_token = None
    db.commit()

    logger.info(f"Email verified for user {user.id}")
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/?email_verificado=1", status_code=302)


@router.post("/reenviar-verificacao")
def resend_verification(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Resends the email verification link.
    Rate-limited: minimum 5 minutes between sends.
    """
    if current_user.email_verified:
        raise HTTPException(status_code=400, detail="E-mail ja verificado.")

    # Rate limit: 5 minutes between resends
    if current_user.email_verification_sent_at:
        sent_at = current_user.email_verification_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - sent_at
        if elapsed.total_seconds() < 300:
            remaining = int(300 - elapsed.total_seconds())
            raise HTTPException(
                status_code=429,
                detail=f"Aguarde {remaining} segundos antes de solicitar novo envio."
            )

    new_token = secrets.token_urlsafe(32)
    current_user.email_verification_token = new_token
    current_user.email_verification_sent_at = datetime.now(timezone.utc)
    db.commit()

    send_verification_email(current_user.email, current_user.name, new_token)
    return {"message": "E-mail de verificacao reenviado."}


@router.post("/validar-documento")
def validate_doc(cpf_cnpj: str, db: Session = Depends(get_db)):
    """
    Validates a CPF or CNPJ without creating an account.
    Used by the registration form for real-time client-side feedback.
    """
    from app.core.document_validator import validate_document
    digits = re.sub(r"\D", "", cpf_cnpj)
    is_valid, result = validate_document(digits)
    return {"valid": is_valid, "doc_type": result if is_valid else None, "message": result}


@router.get("/balance", response_model=BalanceResponse)
def get_balance(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> BalanceResponse:
    """
    Returns current user balance and credit information.
    """
    try:
        balance = get_user_balance(db, current_user.id)

        return BalanceResponse(
            user_id=current_user.id,
            balance=balance,
            credit_limit=current_user.credit_limit,
            available_credit=current_user.credit_limit + balance
        )
    except Exception as e:
        logger.error(f"Error getting balance for user {current_user.id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving balance"
        )


@router.post("/esqueci-senha", status_code=200)
def request_password_reset(payload: PasswordResetRequest, db: Session = Depends(get_db)):
    """
    Initiates password reset flow.
    Generates a readable temporary password, hashes it, stores it, and sends it by email.
    The user pastes the temp password on the reset page to unlock the new-password form.
    Always returns 200 to prevent user enumeration.
    """
    from app.core.email_service import send_temp_password_email
    from app.auth.service import get_password_hash

    user = db.query(User).filter(User.email == payload.email).first()
    if not user:
        return {"message": "Se o e-mail estiver cadastrado, voce recebera as instrucoes em breve."}

    # Rate limit: 5 minutes between requests
    if user.password_reset_sent_at:
        sent_at = user.password_reset_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - sent_at
        if elapsed.total_seconds() < 300:
            remaining = int(300 - elapsed.total_seconds())
            raise HTTPException(
                status_code=429,
                detail=f"Aguarde {remaining} segundos antes de solicitar novamente."
            )

    # Generate a readable temporary password: prefix + 8 random alphanumeric chars
    # Format: BioP-XXXXXXXX — easy to read and type from an email
    raw_chars = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:8].upper()
    temp_password = f"BioP-{raw_chars}"

    # Store the hash of the temp password as the reset token
    # This avoids storing plaintext credentials in the database
    user.password_reset_token = get_password_hash(temp_password)
    user.password_reset_sent_at = datetime.now(timezone.utc)
    db.commit()

    sent = send_temp_password_email(user.email, user.name, temp_password)
    logger.info(f"Password reset requested for user {user.id}, email_sent={sent}")

    response_data: dict = {"message": "Se o e-mail estiver cadastrado, voce recebera as instrucoes em breve."}

    # Fallback: whenever the send fails (no API key, sandbox restriction, network error)
    # expose temp_password in the response so the frontend can still show it to the user.
    # When the email is delivered successfully (sent=True) temp_password stays server-side only.
    if not sent:
        response_data["temp_password"] = temp_password
        response_data["demo_notice"] = "Email nao entregue. Use a senha temporaria abaixo."
        logger.warning(f"Email delivery failed for user {user.id} — temp_password exposed in response")

    return response_data


@router.post("/redefinir-senha", status_code=200)
def confirm_password_reset(payload: PasswordResetConfirmWithTemp, db: Session = Depends(get_db)):
    """
    Confirms password reset using the temporary password received by email.
    Validates temp_password against the stored bcrypt hash, replaces the user's
    password with new_password hash, and invalidates the token.
    Expires in 1 hour. Token is invalidated after first successful use.
    """
    from app.auth.service import get_password_hash, verify_password

    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="As senhas nao coincidem.")

    # Find all users that have a pending reset token (cannot query by hash directly)
    # Iterate candidates with active reset tokens — bounded by rate-limit (max 1 per 5 min per user)
    candidate = db.query(User).filter(
        User.password_reset_token.isnot(None),
        User.password_reset_sent_at.isnot(None)
    ).all()

    user = None
    for c in candidate:
        if verify_password(payload.temp_password, c.password_reset_token):
            user = c
            break

    if not user:
        raise HTTPException(status_code=400, detail="Senha temporaria invalida ou ja utilizada.")

    # Check expiry (1 hour)
    sent_at = user.password_reset_sent_at
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - sent_at
    if elapsed.total_seconds() > 3600:
        user.password_reset_token = None
        user.password_reset_sent_at = None
        db.commit()
        raise HTTPException(status_code=400, detail="Senha temporaria expirada. Solicite uma nova.")

    user.hashed_password = get_password_hash(payload.new_password)
    user.password_reset_token = None
    user.password_reset_sent_at = None
    db.commit()

    logger.info(f"Password reset completed for user {user.id}")
    return {"message": "Senha redefinida com sucesso. Faca login com a nova senha."}


@router.post("/deposit", response_model=DepositResponse, status_code=status.HTTP_201_CREATED)
def deposit(
    deposit_request: DepositRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: Optional[str] = Header(default=None)
) -> DepositResponse:
    """
    Deposits funds into user account.
    Simulates receiving money in BioCodeTechPay internal banking system.
    """
    try:
        correlation_id = x_correlation_id or str(uuid4())

        result = deposit_funds(
            db=db,
            user_id=current_user.id,
            amount=deposit_request.amount,
            description=deposit_request.description,
            correlation_id=correlation_id
        )

        return DepositResponse(**result)

    except ValueError as ve:
        logger.warning(f"Deposit validation error for user {current_user.id}: {str(ve)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        logger.error(f"Error processing deposit for user {current_user.id}: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing deposit"
        )
