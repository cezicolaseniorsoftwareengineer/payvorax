"""
UserSubscription — domain model for the R$9.90/month account manager subscription.
Backed by table `user_subscriptions`. Tracks status, payment method and renewal cycle.
"""
from sqlalchemy import String, Float, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from uuid import uuid4
import enum
from typing import Optional
from app.core.database import Base


def _values(enum_cls):
    return [e.value for e in enum_cls]


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "ATIVO"
    INACTIVE = "INATIVO"
    EXPIRED = "EXPIRADO"


class PaymentMethod(str, enum.Enum):
    BALANCE = "SALDO"
    CREDIT_CARD = "CARTAO"


class UserSubscription(Base):
    """One subscription record per user. Upserted on every activation."""

    __tablename__ = "user_subscriptions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, values_callable=_values),
        nullable=False,
        default=SubscriptionStatus.INACTIVE,
    )
    plan_amount: Mapped[float] = mapped_column(Float, default=9.90, nullable=False)
    payment_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    card_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("credit_cards.id"), nullable=True
    )
    subscribed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_renewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
