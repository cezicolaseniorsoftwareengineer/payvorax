"""
Pydantic schemas for PIX transaction validation.
Enforces financial business rules and format validation.
"""
from pydantic import BaseModel, Field, field_validator, ConfigDict, ValidationInfo
from typing import Optional
from datetime import datetime
from enum import Enum
import re


from app.pix.models import PixStatus

class PixKeyType(str, Enum):
    """Valid PIX key types."""
    CPF = "CPF"
    CNPJ = "CNPJ"
    EMAIL = "EMAIL"
    PHONE = "TELEFONE"
    RANDOM = "ALEATORIA"




class PixCreateRequest(BaseModel):
    """PIX creation request payload."""
    value: float = Field(..., gt=0, le=1000000000000, description="Transaction value (R$)")
    key_type: PixKeyType = Field(..., description="PIX Key Type")
    pix_key: str = Field(..., min_length=1, max_length=200, description="Destination PIX Key")
    description: Optional[str] = Field(None, max_length=500, description="Transaction description")
    scheduled_date: Optional[datetime] = Field(default=None, description="Date for scheduled transfer")

    @field_validator('scheduled_date')
    @classmethod
    def validate_scheduled_date(cls, v: Optional[datetime], info: ValidationInfo) -> Optional[datetime]:
        if v and v.date() < datetime.now().date():
            raise ValueError('Scheduled date cannot be in the past')
        return v

    @field_validator('pix_key')
    @classmethod
    def validate_pix_key(cls, v: str, info: ValidationInfo) -> str:
        """Validates PIX key format based on the selected key type (Strategy Pattern)."""
        if not info.data or 'key_type' not in info.data:
            return v

        tipo = info.data['key_type']

        if tipo == PixKeyType.CPF:
            # Remove formatting
            cpf = re.sub(r'\D', '', v)
            if len(cpf) != 11:
                raise ValueError('CPF must have 11 digits')

        elif tipo == PixKeyType.CNPJ:
            # Remove formatting
            cnpj = re.sub(r'\D', '', v)
            if len(cnpj) != 14:
                raise ValueError('CNPJ must have 14 digits')

        elif tipo == PixKeyType.EMAIL:
            if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', v):
                raise ValueError('Invalid Email format')

        elif tipo == PixKeyType.PHONE:
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
    value: float
    pix_key: str
    key_type: str
    type: str  # SENT or RECEIVED
    status: PixStatus
    description: Optional[str]
    scheduled_date: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    # Detailed Receipt Fields
    sender_name: Optional[str] = None
    sender_doc: Optional[str] = None
    receiver_name: Optional[str] = None
    receiver_doc: Optional[str] = None
    formatted_time: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PixStatementResponse(BaseModel):
    """Transaction ledger response payload."""
    total_transactions: int
    total_value: float
    balance: float
    transactions: list[PixResponse]


class PixChargeRequest(BaseModel):
    """Request payload for generating a PIX charge (Receive)."""
    value: float = Field(..., gt=0, description="Value to receive (R$)")
    description: Optional[str] = Field(None, max_length=100, description="Description for the payer")


class PixChargeResponse(BaseModel):
    """Response payload for a PIX charge."""
    charge_id: str = Field(..., description="Unique Charge ID (Transaction ID)")
    value: float
    description: Optional[str]
    copy_and_paste: str = Field(..., description="Pix Copy and Paste string")
    qr_code_url: str = Field(..., description="URL to generate QR Code image")


class PixChargeConfirmRequest(BaseModel):
    """Request payload for confirming a PIX charge payment."""
    charge_id: str = Field(..., description="Unique Charge ID to confirm")

