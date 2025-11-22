"""
Pydantic schemas for fraud analysis.
Enforces strict input validation and format constraints.
"""
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class AntifraudTransaction(BaseModel):
    """Fraud analysis request payload."""
    value: float = Field(..., gt=0, description="Transaction value (R$)")
    time: str = Field(..., description="Transaction time (HH:MM)")
    attempts_last_24h: int = Field(..., ge=0, description="Attempts in last 24h")
    transaction_type: str = Field(default="PIX", description="Transaction type")
    origin: Optional[str] = Field(None, description="Transaction origin")

    @field_validator('time')
    @classmethod
    def validate_time(cls, v: str) -> str:
        """Validates time format (HH:MM) and logical constraints."""
        try:
            hour, minute = map(int, v.split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            return v
        except Exception:
            raise ValueError('Invalid time format. Use HH:MM')

    @field_validator('attempts_last_24h')
    @classmethod
    def validate_attempts(cls, v: int) -> int:
        """Sanity check for attempt counters to prevent integer overflow or DoS."""
        if v > 100:
            raise ValueError('Number of attempts exceeds reasonable limit')
        return v


class AntifraudResult(BaseModel):
    """Fraud analysis result payload."""
    score: int = Field(..., ge=0, le=100, description="Risk Score (0-100)")
    approved: bool = Field(..., description="Approval status")
    reason: str = Field(..., description="Decision reason")
    triggered_rules: List[str] = Field(..., description="Rules contributing to score")
    risk_level: str = Field(..., description="Risk Level: LOW, MEDIUM, HIGH")
    recommendation: str = Field(..., description="Action recommendation")
