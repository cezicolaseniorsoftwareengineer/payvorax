"""
Data models for PIX transactions.
Supports idempotency, state tracking, and audit trails.
"""
from sqlalchemy import Float, String, DateTime, Enum
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
import enum
from typing import Any, List
from app.core.database import Base


def get_enum_values(enum_cls: Any) -> List[str]:
    """Helper to get values from an Enum class for SQLAlchemy."""
    return [e.value for e in enum_cls]


class PixStatus(str, enum.Enum):
    """Enumeration of valid transaction states."""
    CREATED = "CRIADO"
    PROCESSING = "PROCESSANDO"
    CONFIRMED = "CONFIRMADO"
    FAILED = "FALHOU"
    CANCELED = "CANCELADO"
    SCHEDULED = "AGENDADO"


class TransactionType(str, enum.Enum):
    """Type of transaction."""
    SENT = "ENVIADO"
    RECEIVED = "RECEBIDO"


class PixTransaction(Base):
    """Entity representing a PIX transaction with idempotency constraints."""

    __tablename__ = "transacoes_pix"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)  # UUID
    value: Mapped[float] = mapped_column("valor", Float, nullable=False)
    pix_key: Mapped[str] = mapped_column("chave_pix", String(200), nullable=False, index=True)
    key_type: Mapped[str] = mapped_column("tipo_chave", String(20), nullable=False)  # CPF, EMAIL, PHONE, RANDOM
    type: Mapped[TransactionType] = mapped_column(
        "tipo",
        Enum(TransactionType, values_callable=get_enum_values),
        nullable=False,
        default=TransactionType.SENT
    )
    status: Mapped[PixStatus] = mapped_column(
        "status",
        Enum(PixStatus, values_callable=get_enum_values),
        nullable=False,
        default=PixStatus.CREATED,
        index=True
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)  # Foreign Key to User
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column("descricao", String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column("criado_em", DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        "atualizado_em",
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )
    correlation_id: Mapped[str] = mapped_column(String(100), index=True, nullable=True)
    scheduled_date: Mapped[datetime] = mapped_column("data_agendamento", DateTime, nullable=True)

    def __repr__(self):
        return f"<PixTransaction(id={self.id}, value={self.value}, status={self.status}, type={self.type})>"
