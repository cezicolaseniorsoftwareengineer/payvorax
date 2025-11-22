"""
Business logic for installment calculation.
Implements Price Table (compound interest) and Total Effective Cost (CET) algorithms.
"""
import json
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from app.parcelamento.models import InstallmentSimulation
from app.parcelamento.schemas import SimulationRequest
from app.core.logger import logger, audit_log


def calculate_installments(data: SimulationRequest) -> Dict[str, Any]:
    """
    Calculates amortization schedule using the Price Table method.
    Returns monthly installment, total payable amount, annualized CET, and detailed amortization breakdown.

    Formula: PMT = PV * [(1+i)^n * i] / [(1+i)^n - 1]
    """
    value = data.value
    installments = data.installments
    rate = data.monthly_rate

    # Installment calculation (Price Table)
    factor = (1 + rate) ** installments
    installment = value * (rate * factor) / (factor - 1)

    # Amortization schedule generation
    amortization: List[Dict[str, Any]] = []
    balance = value

    for i in range(installments):
        interest = balance * rate
        principal = installment - interest
        balance -= principal

        # Avoid negative balance due to floating point rounding
        if balance < 0.01:
            balance = 0

        amortization.append({
            "month": i + 1,
            "installment": round(installment, 2),
            "interest": round(interest, 2),
            "principal": round(principal, 2),
            "balance": round(balance, 2)
        })

    # CET (Total Effective Cost) calculation - Annualized
    total_paid = installment * installments
    monthly_cet = (total_paid / value) ** (1 / installments) - 1
    annual_cet = ((1 + monthly_cet) ** 12 - 1) * 100

    logger.info(f"Simulation calculated: value={value}, installments={installments}, installment={round(installment, 2)}")

    return {
        "installment": round(installment, 2),
        "total_paid": round(total_paid, 2),
        "annual_cet": round(annual_cet, 2),
        "table": amortization
    }


def save_simulation(
    db: Session,
    data: SimulationRequest,
    result: Dict[str, Any],
    correlation_id: str
) -> InstallmentSimulation:
    """
    Persists simulation results for audit trails and historical analysis.
    """
    simulation = InstallmentSimulation(
        value=data.value,
        installments=data.installments,
        monthly_rate=data.monthly_rate,
        installment_value=result["installment"],
        total_paid=result["total_paid"],
        annual_cet=result["annual_cet"],
        amortization_table=json.dumps(result["table"]),
        correlation_id=correlation_id
    )

    db.add(simulation)
    db.commit()
    db.refresh(simulation)

    audit_log(
        action="installment_simulation",
        user="system",
        resource=f"simulation_id={simulation.id}",
        details={"correlation_id": correlation_id, "value": data.value, "installments": data.installments}
    )

    logger.info(f"Simulation persisted: id={simulation.id}")

    return simulation
