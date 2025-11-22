"""
FastAPI Router for Anti-Fraud endpoints.
Real-time risk analysis API.
"""
from typing import Annotated, Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Header

from app.antifraude.rules import antifraud_engine
from app.antifraude.schemas import AntifraudResult, AntifraudTransaction
from app.core.logger import audit_log, get_logger_with_correlation

router = APIRouter(tags=["Antifraud"])


@router.post("/analyze", response_model=AntifraudResult)
def analyze_transaction(
    transaction: AntifraudTransaction,
    x_correlation_id: Annotated[Optional[str], Header()] = None
) -> AntifraudResult:
    """
    **Challenge 3: Simplified Anti-Fraud Engine**

    Performs real-time risk scoring.

    - **value**: Transaction value (R$)
    - **time**: Transaction time (HH:MM)
    - **attempts_last_24h**: Velocity check

    **Returns:**
    - Approval decision
    - Risk Score (0-100)
    - Activated Rules
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(
        "Starting anti-fraud analysis: value=%s, time=%s",
        transaction.value,
        transaction.time
    )

    # Execute analysis
    result = antifraud_engine.analyze(transaction)

    # Audit
    audit_log(
        action="antifraud_analysis",
        user="system",
        resource="transaction",
        details={
            "correlation_id": correlation_id,
            "value": transaction.value,
            "score": result["score"],
            "approved": result["approved"],
            "risk_level": result["risk_level"]
        }
    )

    return AntifraudResult(
        score=result["score"],
        approved=result["approved"],
        reason=result["reason"],
        triggered_rules=result["triggered_rules"],
        risk_level=result["risk_level"],
        recommendation=result["recommendation"]
    )


@router.get("/rules", response_model=dict[str, Any])
def list_rules() -> dict[str, Any]:
    """
    Exposes the active rule configuration for transparency and auditability.
    """
    rules: list[dict[str, Any]] = [
        {
            "name": rule.name,
            "points": rule.points,
            "description": rule.description
        }
        for rule in antifraud_engine.rules
    ]

    return {
        "total_rules": len(rules),
        "approval_limit": antifraud_engine.approval_limit,
        "rules": rules
    }
