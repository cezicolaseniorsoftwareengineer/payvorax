from pydantic import BaseModel, Field
from typing import Optional
from datetime import date


class BoletoQuery(BaseModel):
    barcode: str = Field(..., min_length=44, max_length=48, description="Barcode or typeable line")


class BoletoDetails(BaseModel):
    barcode: str
    beneficiary: str
    value: float
    due_date: date
    status: str = "PENDING"


class BoletoPaymentRequest(BaseModel):
    barcode: str
    value: float
    description: Optional[str] = "Boleto Payment"


class PaymentResponse(BaseModel):
    id: str
    status: str
    message: str
    receipt: str
