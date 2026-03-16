"""
Pydantic v2 schemas for the Autonomous Finance Engine.
All models represent read-only financial state — never write targets.
"""
from __future__ import annotations
from pydantic import BaseModel
from datetime import datetime
from typing import List


class TransactionSummary(BaseModel):
    type: str
    amount: float
    date: datetime
    fee: float


class FinancialSnapshot(BaseModel):
    balance: float
    last_30d_received: float
    last_30d_sent: float
    net_cashflow: float
    savings_rate: float
    health_score: int
    total_transactions_30d: int
    recent_transactions: List[TransactionSummary]


class WealthScore(BaseModel):
    score: int
    savings_rate_score: int
    liquidity_score: int
    activity_score: int
    verification_score: int
    label: str
    savings_capacity: float
    emergency_fund_months: float


class CashflowWindow(BaseModel):
    window_days: int
    total_inbound: float
    total_outbound: float
    net_cashflow: float
    avg_daily_outbound: float
    savings_rate: float
    burn_rate_days: float
    alerts: List[str]


class StrategyPlan(BaseModel):
    monthly_savings_target: float
    emergency_fund_target_months: int
    emergency_fund_remaining: float
    investment_suggestion: str
    priority: str
    notes: List[str]


class SimulationResult(BaseModel):
    monthly_investment: float
    annual_rate: float
    year_5: float
    year_10: float
    year_20: float
    year_30: float


class Opportunity(BaseModel):
    opportunity_type: str
    title: str
    location: str
    startup_cost: float
    estimated_roi_months: int
    fit_score: float


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
