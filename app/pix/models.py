"""
Data models for PIX transactions.
Supports idempotency, state tracking, and audit trails.
"""
from sqlalchemy import String, DateTime, Enum, UniqueConstraint, Numeric
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from decimal import Decimal
import enum
from typing import Any, List, Optional
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


class LedgerEntryType(str, enum.Enum):
    """Type of ledger entry."""
    DEBIT = "DEBITO"
    CREDIT = "CREDITO"


class LedgerEntryStatus(str, enum.Enum):
    """Status of ledger entry."""
    PENDING = "PENDENTE"
    SETTLED = "LIQUIDADO"
    REVERSED = "REVERTIDO"


class LedgerEntry(Base):
    """Double-entry ledger for auditable financial mutations."""

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        "tipo_entrada",
        Enum(LedgerEntryType, values_callable=get_enum_values),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column("valor", Numeric(15, 2, asdecimal=True), nullable=False)
    status: Mapped[LedgerEntryStatus] = mapped_column(
        "status",
        Enum(LedgerEntryStatus, values_callable=get_enum_values),
        nullable=False,
        default=LedgerEntryStatus.PENDING,
        index=True,
    )
    tx_id: Mapped[str] = mapped_column("transacao_id", String(36), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column("descricao", String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "criado_em", DateTime, default=lambda: datetime.now(timezone.utc),
    )
    settled_at: Mapped[Optional[datetime]] = mapped_column(
        "liquidado_em", DateTime, nullable=True,
    )

    def __repr__(self):
        return f"<LedgerEntry(id={self.id}, account={self.account_id}, type={self.entry_type}, amount={self.amount}, status={self.status})>"


class PixTransaction(Base):
    """Entity representing a PIX transaction with idempotency constraints."""

    __tablename__ = "transacoes_pix"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)  # UUID
    value: Mapped[Decimal] = mapped_column("valor", Numeric(15, 2, asdecimal=True), nullable=False)
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
    recipient_name: Mapped[str] = mapped_column("nome_destinatario", String(200), nullable=True)
    fee_amount: Mapped[Decimal] = mapped_column("taxa_valor", Numeric(15, 2, asdecimal=True), nullable=True)
    copy_paste_code: Mapped[str] = mapped_column("copy_paste_code", String(2000), nullable=True)
    expires_at: Mapped[datetime] = mapped_column("link_expires_at", DateTime, nullable=True)
    # SHA-256 (hex, 64 chars) of the normalized EMV payload used for server-side deduplication.
    # Composite unique constraint prevents the same user from paying the same QR twice
    # regardless of what idempotency header the frontend sends.
    payload_hash: Mapped[Optional[str]] = mapped_column("payload_hash", String(64), nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("user_id", "payload_hash", name="uix_pix_user_payload_hash"),
    )

    def __repr__(self):
        return f"<PixTransaction(id={self.id}, value={self.value}, status={self.status}, type={self.type})>"
