"""
FastAPI Router for installment simulation endpoints.
Exposes RESTful API with strict validation and automated documentation.
"""
import json
from uuid import uuid4
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.parcelamento.schemas import SimulacaoRequest, SimulacaoResponse, ParcelaAmortizacao
from app.parcelamento.service import calcular_parcelas, salvar_simulacao
from app.parcelamento.models import SimulacaoParcelamento
from app.core.database import get_db
from app.core.logger import get_logger_with_correlation
from app.auth.dependencies import require_active_account
from app.auth.models import User

router = APIRouter(tags=["Parcelamento"])


@router.post("/simular", response_model=SimulacaoResponse, status_code=201)
def simular_parcelamento(
    dados: SimulacaoRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_active_account),
    x_correlation_id: str = Header(default=None)
) -> SimulacaoResponse:
    """
    **Challenge 1: Installment Simulation Engine**

    Calculates compound interest (Price Table) and CET.
    **Requires active account (at least one deposit made).**

    - **valor**: Principal amount (R$)
    - **parcelas**: Number of installments (1-360)
    - **taxa_mensal**: Monthly interest rate in decimal (e.g., 0.035 = 3.5%)

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
        logger.info(f"Starting simulation: {dados.model_dump()}")

        # Installment calculation
        resultado: Dict[str, Any] = calcular_parcelas(dados)

        # Persistence for audit
        simulacao = salvar_simulacao(db, dados, resultado, correlation_id)

        # Conversion to response schema
        response = SimulacaoResponse(
            parcela=resultado["parcela"],
            total_pago=resultado["total_pago"],
            cet_anual=resultado["cet_anual"],
            tabela=[ParcelaAmortizacao(**item) for item in resultado["tabela"]],
            simulacao_id=simulacao.id,
            criado_em=simulacao.criado_em
        )

        logger.info(f"Simulation completed successfully: id={simulacao.id}")
        return response

    except Exception as e:
        logger.error(f"Simulation error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing simulation: {str(e)}")


@router.get("/historico/{simulacao_id}", response_model=SimulacaoResponse)
def buscar_simulacao(
    simulacao_id: int,
    db: Session = Depends(get_db)
) -> SimulacaoResponse:
    """
    Retrieves simulation history by ID for audit purposes.
    """
    simulacao = db.query(SimulacaoParcelamento).filter(
        SimulacaoParcelamento.id == simulacao_id
    ).first()

    if not simulacao:
        raise HTTPException(status_code=404, detail="Simulation not found")

    tabela_data: List[Dict[str, Any]] = json.loads(simulacao.tabela_amortizacao)

    return SimulacaoResponse(
        parcela=simulacao.valor_parcela,
        total_pago=simulacao.total_pago,
        cet_anual=simulacao.cet_anual,
        tabela=[ParcelaAmortizacao(**item) for item in tabela_data],
        simulacao_id=simulacao.id,
        criado_em=simulacao.criado_em
    )
