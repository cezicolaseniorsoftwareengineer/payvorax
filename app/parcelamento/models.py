"""
Data models for installment simulations.
Persists historical data for audit and analytics.
"""
from sqlalchemy import Integer, Float, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from app.core.database import Base


class InstallmentSimulation(Base):
    """Entity representing a performed installment simulation."""

    __tablename__ = "simulacoes_parcelamento"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    value: Mapped[float] = mapped_column("valor", Float, nullable=False)
    installments: Mapped[int] = mapped_column("parcelas", Integer, nullable=False)
    monthly_rate: Mapped[float] = mapped_column("taxa_mensal", Float, nullable=False)
    installment_value: Mapped[float] = mapped_column("valor_parcela", Float, nullable=False)
    total_paid: Mapped[float] = mapped_column("total_pago", Float, nullable=False)
    annual_cet: Mapped[float] = mapped_column("cet_anual", Float, nullable=False)
    amortization_table: Mapped[str] = mapped_column("tabela_amortizacao", Text, nullable=False)  # Serialized JSON
    created_at: Mapped[datetime] = mapped_column("criado_em", DateTime, default=lambda: datetime.now(timezone.utc))
    correlation_id: Mapped[str] = mapped_column(String(100), index=True, nullable=True)

    def __repr__(self):
        return f"<InstallmentSimulation(id={self.id}, value={self.value}, installments={self.installments})>"
