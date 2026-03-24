"""
Financial Snapshot Service — read-only aggregation layer.
Builds FinancialSnapshot from DB data. Never writes. No PII exposed beyond this boundary.
"""
from datetime import datetime, timezone, timedelta
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.boleto.models import BoletoTransaction, BoletoStatus
from app.ia.schemas import FinancialSnapshot, TransactionSummary
from app.minha_conta.service import get_financial_health


def build_snapshot(db: Session, user: User, window_days: int = 30) -> FinancialSnapshot:
    """
    Builds a financial snapshot for the given user.
    All DB access is read-only. No PII (CPF/CNPJ, names, keys) leaves this function.
    Uses timezone-naive UTC datetime for cutoff to avoid PostgreSQL TIMESTAMP WITHOUT TIME ZONE
    comparison errors (SQLite is permissive; PostgreSQL raises ProgrammingError on mismatch).
    """
    cutoff = datetime.utcnow() - timedelta(days=window_days)

    received_30d: float = (
        db.query(func.sum(PixTransaction.value))
        .filter(
            PixTransaction.user_id == user.id,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.status == PixStatus.CONFIRMED,
            PixTransaction.created_at >= cutoff,
        )
        .scalar()
        or 0.0
    )

    sent_30d: float = (
        db.query(func.sum(PixTransaction.value))
        .filter(
            PixTransaction.user_id == user.id,
            PixTransaction.type == TransactionType.SENT,
            PixTransaction.status == PixStatus.CONFIRMED,
            PixTransaction.created_at >= cutoff,
        )
        .scalar()
        or 0.0
    )

    boleto_30d: float = (
        db.query(func.sum(BoletoTransaction.value))
        .filter(
            BoletoTransaction.user_id == user.id,
            BoletoTransaction.status == BoletoStatus.PAID,
            BoletoTransaction.created_at >= cutoff,
        )
        .scalar()
        or 0.0
    )

    total_sent = sent_30d + boleto_30d
    net = received_30d - total_sent
    savings_rate = (net / received_30d) if received_30d > 0 else 0.0

    tx_count: int = (
        db.query(func.count(PixTransaction.id))
        .filter(
            PixTransaction.user_id == user.id,
            PixTransaction.created_at >= cutoff,
        )
        .scalar()
        or 0
    )

    recent_rows = (
        db.query(PixTransaction)
        .filter(PixTransaction.user_id == user.id)
        .order_by(PixTransaction.created_at.desc())
        .limit(5)
        .all()
    )

    recent: List[TransactionSummary] = [
        TransactionSummary(
            type=tx.type.value if hasattr(tx.type, "value") else str(tx.type),
            amount=tx.value,
            date=tx.created_at,
            fee=tx.fee_amount or 0.0,
        )
        for tx in recent_rows
    ]

    health = get_financial_health(db, user)

    return FinancialSnapshot(
        balance=user.balance,
        last_30d_received=received_30d,
        last_30d_sent=total_sent,
        net_cashflow=net,
        savings_rate=round(savings_rate, 4),
        health_score=health["health_score"],
        total_transactions_30d=tx_count,
        recent_transactions=recent,
    )
