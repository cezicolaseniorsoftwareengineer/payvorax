"""
FastAPI Router for installment simulation endpoints.
Exposes RESTful API with strict validation and automated documentation.
"""
import json
from uuid import uuid4
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.parcelamento.schemas import SimulationRequest, SimulationResponse, AmortizationInstallment
from app.parcelamento.service import calculate_installments, save_simulation
from app.parcelamento.models import InstallmentSimulation
from app.core.database import get_db
from app.core.logger import get_logger_with_correlation
from app.auth.dependencies import require_active_account
from app.auth.models import User

router = APIRouter(tags=["Installments"])


@router.post("/simulate", response_model=SimulationResponse, status_code=201)
def simulate_installments(
    data: SimulationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_active_account),
    x_correlation_id: str = Header(default=None)
) -> SimulationResponse:
    """
    **Challenge 1: Installment Simulation Engine**

    Calculates compound interest (Price Table) and CET.
    **Requires active account (at least one deposit made).**

    - **value**: Principal amount (R$)
    - **installments**: Number of installments (1-360)
    - **monthly_rate**: Monthly interest rate in decimal (e.g., 0.035 = 3.5%)

    **Returns:**
    - Monthly installment value
    - Total payable amount
    - Annualized CET (%)
    - Full amortization schedule
    """
    # Generate correlation_id for traceability
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        logger.info(f"Starting simulation: {data.model_dump()}")

        # Installment calculation
        result: Dict[str, Any] = calculate_installments(data)

        # Persistence for audit
        simulation = save_simulation(db, data, result, correlation_id)

        # Conversion to response schema
        response = SimulationResponse(
            installment=result["installment"],
            total_paid=result["total_paid"],
            annual_cet=result["annual_cet"],
            table=[AmortizationInstallment(**item) for item in result["table"]],
            simulation_id=simulation.id,
            created_at=simulation.created_at
        )

        logger.info(f"Simulation completed successfully: id={simulation.id}")
        return response

    except Exception as e:
        logger.error(f"Simulation error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing simulation: {str(e)}")


@router.get("/history/{simulation_id}", response_model=SimulationResponse)
def get_simulation(
    simulation_id: int,
    db: Session = Depends(get_db)
) -> SimulationResponse:
    """
    Retrieves simulation history by ID for audit purposes.
    """
    simulation = db.query(InstallmentSimulation).filter(
        InstallmentSimulation.id == simulation_id
    ).first()

    if not simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

    table_data: List[Dict[str, Any]] = json.loads(simulation.amortization_table)

    return SimulationResponse(
        installment=simulation.installment_value,
        total_paid=simulation.total_paid,
        annual_cet=simulation.annual_cet,
        table=[AmortizationInstallment(**item) for item in table_data],
        simulation_id=simulation.id,
        created_at=simulation.created_at
    )
