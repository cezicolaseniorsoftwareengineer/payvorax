from pydantic import BaseModel, EmailStr, Field, field_validator
import re


class UserCreate(BaseModel):
    name: str = Field(..., min_length=3)
    cpf_cnpj: str = Field(..., min_length=11)
    email: EmailStr
    password: str = Field(..., min_length=6)

    @field_validator('cpf_cnpj')
    @classmethod
    def validate_cpf_cnpj(cls, v: str) -> str:
        return re.sub(r'\D', '', v)


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
