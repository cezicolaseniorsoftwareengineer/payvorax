"""
Pydantic schemas for PIX transaction validation.
Enforces financial business rules and format validation.
"""
from pydantic import BaseModel, Field, field_validator, ConfigDict, ValidationInfo
from typing import Optional
from datetime import datetime
from enum import Enum
import re


class TipoChavePix(str, Enum):
    """Valid PIX key types."""
    CPF = "CPF"
    CNPJ = "CNPJ"
    EMAIL = "EMAIL"
    TELEFONE = "TELEFONE"
    ALEATORIA = "ALEATORIA"


class StatusPix(str, Enum):
    """Transaction status enumeration."""
    CRIADO = "CRIADO"
    PROCESSANDO = "PROCESSANDO"
    CONFIRMADO = "CONFIRMADO"
    FALHOU = "FALHOU"
    CANCELADO = "CANCELADO"
    AGENDADO = "AGENDADO"


class PixCreateRequest(BaseModel):
    """PIX creation request payload."""
    valor: float = Field(..., gt=0, le=1000000000000, description="Transaction value (R$)")
    tipo_chave: TipoChavePix = Field(..., description="PIX Key Type")
    chave_pix: str = Field(..., min_length=1, max_length=200, description="Destination PIX Key")
    descricao: Optional[str] = Field(None, max_length=500, description="Transaction description")
    data_agendamento: Optional[datetime] = Field(default=None, description="Date for scheduled transfer")

    @field_validator('data_agendamento')
    @classmethod
    def validar_data_agendamento(cls, v: Optional[datetime], info: ValidationInfo) -> Optional[datetime]:
        if v and v.date() < datetime.now().date():
            raise ValueError('Data de agendamento não pode ser no passado')
        return v

    @field_validator('chave_pix')
    @classmethod
    def validar_chave_pix(cls, v: str, info: ValidationInfo) -> str:
        """Validates PIX key format based on the selected key type (Strategy Pattern)."""
        if not info.data or 'tipo_chave' not in info.data:
            return v

        tipo = info.data['tipo_chave']

        if tipo == TipoChavePix.CPF:
            # Remove formatting
            cpf = re.sub(r'\D', '', v)
            if len(cpf) != 11:
                raise ValueError('CPF must have 11 digits')

        elif tipo == TipoChavePix.EMAIL:
            if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', v):
                raise ValueError('Invalid Email format')

        elif tipo == TipoChavePix.TELEFONE:
            telefone = re.sub(r'\D', '', v)
            if len(telefone) < 10 or len(telefone) > 11:
                raise ValueError('Phone number must have 10 or 11 digits')

        return v


class PixConfirmRequest(BaseModel):
    """PIX confirmation request payload."""
    pix_id: str = Field(..., description="Transaction ID")


class PixResponse(BaseModel):
    """Transaction details response payload."""
    id: str
    valor: float
    chave_pix: str
    tipo_chave: str
    tipo: str  # ENVIADO or RECEBIDO
    status: StatusPix
    descricao: Optional[str]
    data_agendamento: Optional[datetime]
    criado_em: datetime
    atualizado_em: datetime

    model_config = ConfigDict(from_attributes=True)


class PixStatementResponse(BaseModel):
    """Transaction ledger response payload."""
    total_transacoes: int
    total_valor: float
    saldo: float
    transacoes: list[PixResponse]


class PixChargeRequest(BaseModel):
    """Request payload for generating a PIX charge (Receive)."""
    valor: float = Field(..., gt=0, description="Value to receive (R$)")
    descricao: Optional[str] = Field(None, max_length=100, description="Description for the payer")


class PixChargeResponse(BaseModel):
    """Response payload for a PIX charge."""
    valor: float
    descricao: Optional[str]
    copia_e_cola: str = Field(..., description="Pix Copy and Paste string")
    qr_code_url: str = Field(..., description="URL to generate QR Code image")
