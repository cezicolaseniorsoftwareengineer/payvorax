from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from app.cards.models import CardType

class CardCreateRequest(BaseModel):
    type: CardType = Field(..., description="Type of card to create")
    alias: Optional[str] = Field(None, description="Nickname for the card")

class CardUpdateLimitRequest(BaseModel):
    limit: float = Field(..., gt=0, description="New limit for the card")

class CardResponse(BaseModel):
    id: str
    card_number: str
    cvv: str
    expiration_date: str
    card_holder_name: str
    type: str
    is_blocked: bool
    limit: float
    created_at: datetime
    expires_at: Optional[datetime]

    class Config:
        from_attributes = True
