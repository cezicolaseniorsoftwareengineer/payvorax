from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Boolean, Enum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.core.database import Base

class CardType(str, enum.Enum):
    PHYSICAL = "PHYSICAL"
    VIRTUAL_MULTUSE = "VIRTUAL_MULTUSE"
    VIRTUAL_TEMP = "VIRTUAL_TEMP"

class CreditCard(Base):
    __tablename__ = "credit_cards"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)

    card_number = Column(String, nullable=False)  # Stored masked or encrypted in real world, plain for MVP
    cvv = Column(String, nullable=False)
    expiration_date = Column(String, nullable=False) # MM/YY
    card_holder_name = Column(String, nullable=False)

    type = Column(String, nullable=False, default=CardType.VIRTUAL_MULTUSE)
    is_blocked = Column(Boolean, default=False)
    limit = Column(Float, default=0.0)  # Specific limit for this card

    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True) # For temp cards

    user = relationship("User", back_populates="cards")
