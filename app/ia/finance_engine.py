"""
Finance Engine — deterministic financial intelligence.
All functions are pure calculations. No DB access. No LLM calls.
Every result is auditable and independently testable.
"""
from __future__ import annotations
from typing import List
from app.ia.schemas import (
    FinancialSnapshot,
    WealthScore,
    CashflowWindow,
    StrategyPlan,
    SimulationResult,
    Opportunity,
)
from app.auth.models import User


# ---------------------------------------------------------------------------
# Wealth Score Engine
# ---------------------------------------------------------------------------

def compute_wealth_score(
    snapshot: FinancialSnapshot,
    email_verified: bool = False,
    doc_verified: bool = False,
) -> WealthScore:
    """
    Deterministic wealth score 0-100.
    Breakdown:
      savings_rate_score  0-40  (40% weight)
      liquidity_score     0-30  (30% weight)
      activity_score      0-15  (15% weight)
      verification_score  0-15  (15% weight)
    """
    sr = snapshot.savings_rate
    if sr >= 0.30:
        sr_score = 40
    elif sr >= 0.20:
        sr_score = 30
    elif sr >= 0.10:
        sr_score = 20
    elif sr > 0:
        sr_score = 10
    else:
        sr_score = 0

    monthly_expenses = snapshot.last_30d_sent if snapshot.last_30d_sent > 0 else 1.0
    em_months = snapshot.balance / monthly_expenses
    if em_months >= 6:
        liq_score = 30
    elif em_months >= 3:
        liq_score = 20
    elif em_months >= 1:
        liq_score = 10
    else:
        liq_score = 0

    tx = snapshot.total_transactions_30d
    if tx >= 20:
        act_score = 15
    elif tx >= 10:
        act_score = 10
    elif tx >= 3:
        act_score = 5
    else:
        act_score = 0

    ver_score = 0
    if email_verified:
        ver_score += 8
    if doc_verified:
        ver_score += 7

    total = min(sr_score + liq_score + act_score + ver_score, 100)

    if total >= 80:
        label = "Excelente"
    elif total >= 60:
        label = "Bom"
    elif total >= 40:
        label = "Regular"
    else:
        label = "Atencao"

    return WealthScore(
        score=total,
        savings_rate_score=sr_score,
        liquidity_score=liq_score,
        activity_score=act_score,
        verification_score=ver_score,
        label=label,
        savings_capacity=round(max(snapshot.net_cashflow, 0.0), 2),
        emergency_fund_months=round(em_months, 2),
    )


# ---------------------------------------------------------------------------
# Cashflow Analyzer
# ---------------------------------------------------------------------------

def analyze_cashflow(snapshot: FinancialSnapshot, window_days: int = 30) -> CashflowWindow:
    """
    Cashflow window analysis with deterministic alert rules.
    burn_rate_days: how many days current balance would last at current spend rate.
    """
    outbound = snapshot.last_30d_sent
    avg_daily = outbound / window_days if window_days > 0 else 0.0
    burn_rate = snapshot.balance / avg_daily if avg_daily > 0 else 9999.0

    alerts: List[str] = []
    if snapshot.savings_rate < 0.05:
        alerts.append("LOW_SAVINGS_ALERT: taxa de poupanca abaixo de 5%")
    if burn_rate < 30:
        alerts.append(f"BURN_RATE_CRITICAL: saldo cobre apenas {burn_rate:.0f} dias")
    elif burn_rate < 90:
        alerts.append(f"BURN_RATE_WARNING: saldo cobre {burn_rate:.0f} dias")
    if snapshot.net_cashflow < 0:
        alerts.append("NEGATIVE_CASHFLOW: gastos superaram receitas no periodo")

    return CashflowWindow(
        window_days=window_days,
        total_inbound=snapshot.last_30d_received,
        total_outbound=outbound,
        net_cashflow=snapshot.net_cashflow,
        avg_daily_outbound=round(avg_daily, 2),
        savings_rate=round(snapshot.savings_rate, 4),
        burn_rate_days=round(burn_rate, 1),
        alerts=alerts,
    )


# ---------------------------------------------------------------------------
# Strategy Engine
# ---------------------------------------------------------------------------

_EMERGENCY_TARGET_MONTHS = 6


def generate_strategy(snapshot: FinancialSnapshot, wealth: WealthScore) -> StrategyPlan:
    """
    Rule-based savings and investment strategy.
    Considers income volatility proxy (savings_rate variance) and risk via
    emergency fund coverage. No LLM involved.
    """
    capacity = wealth.savings_capacity
    monthly_expenses = snapshot.last_30d_sent if snapshot.last_30d_sent > 0 else 1.0
    emergency_target = monthly_expenses * _EMERGENCY_TARGET_MONTHS
    emergency_remaining = max(emergency_target - snapshot.balance, 0.0)

    notes: List[str] = []

    if emergency_remaining > 0:
        priority = "emergency_fund_first"
        savings_target = min(capacity * 0.8, emergency_remaining)
        notes.append(f"Priorize reserva de emergencia: faltam R$ {emergency_remaining:.2f} para {_EMERGENCY_TARGET_MONTHS} meses.")
        investment_suggestion = "Renda fixa liquidez diaria (Tesouro Selic ou CDB 100%+ CDI)"
    else:
        priority = "wealth_growth"
        savings_target = capacity * 0.3
        notes.append("Reserva de emergencia completa. Foco em crescimento patrimonial.")
        if capacity >= 2000:
            investment_suggestion = "60% ETFs (BOVA11 / IVVB11) + 40% Tesouro IPCA+"
        elif capacity >= 500:
            investment_suggestion = "Tesouro IPCA+ 2029 ou CDB longo prazo"
        else:
            investment_suggestion = "Tesouro Selic enquanto aumenta capacidade de aporte"

    if snapshot.savings_rate < 0.10:
        notes.append("Revise gastos fixos e variaveis para elevar taxa de poupanca acima de 10%.")

    return StrategyPlan(
        monthly_savings_target=round(savings_target, 2),
        emergency_fund_target_months=_EMERGENCY_TARGET_MONTHS,
        emergency_fund_remaining=round(emergency_remaining, 2),
        investment_suggestion=investment_suggestion,
        priority=priority,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Simulation Engine
# ---------------------------------------------------------------------------

def simulate_wealth_growth(
    monthly_investment: float,
    annual_rate: float = 0.10,
) -> SimulationResult:
    """
    Future value of monthly contributions.
    FV = PMT * ((1 + r)^n - 1) / r
    annual_rate: decimal e.g. 0.10 = 10% a.a.
    """
    r = annual_rate / 12

    def fv(years: int) -> float:
        n = years * 12
        if r == 0:
            return monthly_investment * n
        return monthly_investment * (((1 + r) ** n - 1) / r)

    return SimulationResult(
        monthly_investment=monthly_investment,
        annual_rate=annual_rate,
        year_5=round(fv(5), 2),
        year_10=round(fv(10), 2),
        year_20=round(fv(20), 2),
        year_30=round(fv(30), 2),
    )


# ---------------------------------------------------------------------------
# Opportunity Engine — static catalog (Phase 1)
# ---------------------------------------------------------------------------

_OPPORTUNITY_CATALOG = [
    {"type": "small_business", "title": "Maquininha de cartao como agente autonomo", "startup_cost": 2500.0, "roi_months": 6, "min_capacity": 800.0},
    {"type": "small_business", "title": "Vending machine (snacks / cafe)", "startup_cost": 7000.0, "roi_months": 8, "min_capacity": 3000.0},
    {"type": "investment", "title": "CDB liquidez diaria (110% CDI)", "startup_cost": 500.0, "roi_months": 12, "min_capacity": 300.0},
    {"type": "investment", "title": "Tesouro IPCA+ 2029", "startup_cost": 100.0, "roi_months": 36, "min_capacity": 100.0},
    {"type": "small_business", "title": "Revenda de infoprodutos / cursos digitais", "startup_cost": 500.0, "roi_months": 3, "min_capacity": 300.0},
    {"type": "investment", "title": "ETF BOVA11 — aporte mensal disciplinado", "startup_cost": 100.0, "roi_months": 60, "min_capacity": 200.0},
]


def find_opportunities(snapshot: FinancialSnapshot, user: User) -> List[Opportunity]:
    """
    Returns up to 3 opportunities matching the user savings capacity.
    Fit score = min(capacity / startup_cost, 1.0).
    """
    capacity = max(snapshot.net_cashflow, snapshot.balance * 0.05, 0.0)
    city = getattr(user, "address_city", None) or "Brasil"
    results: List[Opportunity] = []

    for item in _OPPORTUNITY_CATALOG:
        if capacity < item["min_capacity"]:
            continue
        fit = min(capacity / item["startup_cost"], 1.0) if item["startup_cost"] > 0 else 1.0
        results.append(
            Opportunity(
                opportunity_type=item["type"],
                title=item["title"],
                location=city,
                startup_cost=item["startup_cost"],
                estimated_roi_months=item["roi_months"],
                fit_score=round(fit, 3),
            )
        )

    results.sort(key=lambda o: o.fit_score, reverse=True)
    return results[:3]
