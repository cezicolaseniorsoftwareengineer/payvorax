"""
Pydantic schemas for input/output validation.
Enforces strict type checking and boundary constraints.
"""
from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import List
from datetime import datetime


class AmortizationInstallment(BaseModel):
    """Represents a single row in the amortization schedule."""
    month: int = Field(..., ge=1, description="Month number")
    installment: float = Field(..., gt=0, description="Installment value")
    interest: float = Field(..., ge=0, description="Interest amount")
    principal: float = Field(..., ge=0, description="Principal amortization")
    balance: float = Field(..., ge=0, description="Remaining balance")


class SimulationRequest(BaseModel):
    """Installment simulation request payload."""
    value: float = Field(..., gt=0, le=1000000, description="Principal amount")
    installments: int = Field(..., ge=1, le=360, description="Number of installments")
    monthly_rate: float = Field(..., gt=0, le=0.15, description="Monthly interest rate (decimal)")

    @field_validator('monthly_rate')
    @classmethod
    def validate_rate(cls, v: float) -> float:
        if v > 0.15:  # 15% monthly is a practical limit
            raise ValueError('Monthly rate cannot exceed 15%')
        return v


class SimulationResponse(BaseModel):
    """Simulation result payload."""
    installment: float = Field(..., description="Monthly installment value")
    total_paid: float = Field(..., description="Total payable amount")
    annual_cet: float = Field(..., description="Annualized Total Effective Cost (%)")
    table: List[AmortizationInstallment] = Field(..., description="Full amortization schedule")
    simulation_id: int = Field(..., description="Persisted simulation ID")
    created_at: datetime = Field(..., description="Simulation timestamp")

    model_config = ConfigDict(from_attributes=True)
