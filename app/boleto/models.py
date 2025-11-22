from sqlalchemy import Float, String, DateTime, Enum
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
import enum
from typing import Any, List
from app.core.database import Base


def get_enum_values(enum_cls: Any) -> List[str]:
    """Helper to get values from an Enum class for SQLAlchemy."""
    return [e.value for e in enum_cls]


class BoletoStatus(str, enum.Enum):
    PENDING = "PENDENTE"
    PAID = "PAGO"
    FAILED = "FALHOU"


class BoletoTransaction(Base):
    __tablename__ = "transacoes_boleto"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    value: Mapped[float] = mapped_column("valor", Float, nullable=False)
    barcode: Mapped[str] = mapped_column("codigo_barras", String(100), nullable=False)
    description: Mapped[str] = mapped_column("descricao", String(500), nullable=True)
    status: Mapped[BoletoStatus] = mapped_column(
        "status",
        Enum(BoletoStatus, values_callable=get_enum_values),
        nullable=False,
        default=BoletoStatus.PENDING
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column("criado_em", DateTime, default=lambda: datetime.now(timezone.utc))
    correlation_id: Mapped[str] = mapped_column(String(100), nullable=True)
