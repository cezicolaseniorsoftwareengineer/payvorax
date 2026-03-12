from pydantic import BaseModel, EmailStr, Field, field_validator
from datetime import datetime
from typing import Optional
import re


class UserCreate(BaseModel):
    name: str = Field(..., min_length=3)
    cpf_cnpj: str = Field(..., min_length=11)
    email: EmailStr
    password: str = Field(..., min_length=6)
    phone: str = Field(..., min_length=10, description="Telefone com DDD, apenas numeros")
    address_street: str = Field(..., min_length=3, description="Logradouro")
    address_number: str = Field(..., min_length=1, description="Numero")
    address_complement: Optional[str] = Field(default=None, description="Complemento")
    address_city: str = Field(..., min_length=2, description="Cidade")
    address_state: str = Field(..., min_length=2, max_length=2, description="UF (2 letras)")
    address_zip: str = Field(..., min_length=8, description="CEP, apenas numeros")

    @field_validator('cpf_cnpj')
    @classmethod
    def validate_cpf_cnpj(cls, v: str) -> str:
        return re.sub(r'\D', '', v)

    @field_validator('phone')
    @classmethod
    def strip_phone(cls, v: str) -> str:
        return re.sub(r'\D', '', v)

    @field_validator('address_zip')
    @classmethod
    def strip_zip(cls, v: str) -> str:
        return re.sub(r'\D', '', v)

    @field_validator('address_state')
    @classmethod
    def upper_state(cls, v: str) -> str:
        return v.upper().strip()


class UserLogin(BaseModel):
    cpf_cnpj: str
    password: str

    @field_validator('cpf_cnpj')
    @classmethod
    def validate_cpf_cnpj(cls, v: str) -> str:
        return re.sub(r'\D', '', v)


class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    cpf_cnpj: str


class DepositRequest(BaseModel):
    amount: float = Field(..., gt=0, le=1000000, description="Deposit amount in BRL")
    description: str = Field(default="Deposit", max_length=200)


class DepositResponse(BaseModel):
    user_id: str
    amount: float
    previous_balance: float
    new_balance: float
    description: str
    timestamp: datetime


class BalanceResponse(BaseModel):
    user_id: str
    balance: float
    credit_limit: float
    available_credit: float


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(..., min_length=6)


class PasswordResetConfirmWithTemp(BaseModel):
    """Flow with temporary password sent by email (no URL token required)."""
    temp_password: str = Field(..., min_length=1, description="Temporary password received by email")
    new_password: str = Field(..., min_length=6)
    confirm_password: str = Field(..., min_length=6)
