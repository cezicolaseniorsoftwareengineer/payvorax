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
    Registra um novo usuário com validações robustas e tratamento de erros.
    """
    try:
        logger.info(f"Iniciando registro para CPF/CNPJ: {user.cpf_cnpj}")

        # Verificar se usuário já existe
        db_user = db.query(User).filter(
            (User.email == user.email) | (User.cpf_cnpj == user.cpf_cnpj)
        ).first()

        if db_user:
            logger.warning(f"Tentativa de cadastro duplicado: {user.cpf_cnpj} ou {user.email}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email ou CPF/CNPJ já cadastrado no sistema."
            )

        # Criar novo usuário
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

        logger.info(f"Usuário criado com sucesso: ID {new_user.id}")

        # Auto-login: Gerar token
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": new_user.cpf_cnpj, "nome": new_user.nome},
            expires_delta=access_token_expires
        )

        # Configurar Cookie Seguro (HttpOnly)
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
            "message": "Cadastro realizado com sucesso."
        }

    except IntegrityError as e:
        db.rollback()
        logger.error(f"Erro de integridade no banco de dados: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Erro ao processar dados. Verifique se os campos estão corretos."
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Erro interno no registro: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro interno do servidor. Por favor, tente novamente mais tarde."
        )

@router.post("/login")
def login(response: Response, user_in: UserLogin, db: Session = Depends(get_db)):
    """
    Autentica o usuário e define o cookie de sessão.
    """
    try:
        logger.info(f"Tentativa de login para CPF/CNPJ: {user_in.cpf_cnpj}")

        user = db.query(User).filter(User.cpf_cnpj == user_in.cpf_cnpj).first()

        if not user or not verify_password(user_in.password, user.hashed_password):
            logger.warning(f"Falha de login para {user_in.cpf_cnpj}: Credenciais inválidas")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="CPF/CNPJ ou senha incorretos."
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

        logger.info(f"Login bem-sucedido para {user.cpf_cnpj}")

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "nome": user.nome
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Erro interno no login: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao realizar login."
        )

@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Logout realizado com sucesso"}
