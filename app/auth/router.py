from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.core.database import get_db
from app.auth.models import User
from app.auth.schemas import UserCreate, UserLogin
from app.auth.service import get_password_hash, verify_password, create_access_token
from datetime import timedelta
from app.core.config import settings
from app.core.logger import logger
import traceback

router = APIRouter()

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(response: Response, user: UserCreate, db: Session = Depends(get_db)):
    """
    Registers a new user with robust validation and error handling.
    """
    try:
        logger.info(f"Starting registration for CPF/CNPJ: {user.cpf_cnpj}")

        # Check if user already exists
        db_user = db.query(User).filter(
            (User.email == user.email) | (User.cpf_cnpj == user.cpf_cnpj)
        ).first()

        if db_user:
            logger.warning(f"Duplicate registration attempt: {user.cpf_cnpj} or {user.email}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email or CPF/CNPJ already registered in the system."
            )

        # Create new user
        hashed_password = get_password_hash(user.password)
        new_user = User(
            nome=user.nome,
            email=user.email,
            cpf_cnpj=user.cpf_cnpj,
            hashed_password=hashed_password
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        logger.info(f"User created successfully: ID {new_user.id}")

        # Auto-login: Generate token
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": new_user.cpf_cnpj, "nome": new_user.nome},
            expires_delta=access_token_expires
        )

        # Configure Secure Cookie (HttpOnly)
        response.set_cookie(
            key="access_token",
            value=f"Bearer {access_token}",
            httponly=True,
            secure=True,  # Requer HTTPS
            samesite="lax",
            max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            expires=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "nome": new_user.nome,
            "message": "Registration successful."
        }

    except IntegrityError as e:
        db.rollback()
        logger.error(f"Database integrity error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error processing data. Check if fields are correct."
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Internal registration error: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error. Please try again later."
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
            data={"sub": user.cpf_cnpj, "nome": user.nome},
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
            "nome": user.nome
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
