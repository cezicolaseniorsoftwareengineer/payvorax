"""
FastAPI Router for Anti-Fraud endpoints.
Real-time risk analysis API.
"""
from typing import Annotated, Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Header

from app.antifraude.rules import motor_antifraude
from app.antifraude.schemas import ResultadoAntifraude, TransacaoAntifraude
from app.core.logger import audit_log, get_logger_with_correlation

router = APIRouter(tags=["Antifraude"])


@router.post("/analisar", response_model=ResultadoAntifraude)
def analisar_transacao(
    transacao: TransacaoAntifraude,
    x_correlation_id: Annotated[Optional[str], Header()] = None
) -> ResultadoAntifraude:
    """
    **Challenge 3: Simplified Anti-Fraud Engine**

    Performs real-time risk scoring.

    - **valor**: Transaction value (R$)
    - **horario**: Transaction time (HH:MM)
    - **tentativas_ultimas_24h**: Velocity check

    **Returns:**
    - Approval decision
    - Risk Score (0-100)
    - Activated Rules
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(
        "Starting anti-fraud analysis: value=%s, time=%s",
        transacao.valor,
        transacao.horario
    )

    # Execute analysis
    resultado = motor_antifraude.analisar(transacao)

    # Audit
    audit_log(
        action="analise_antifraude",
        user="sistema",
        resource="transacao",
        details={
            "correlation_id": correlation_id,
            "valor": transacao.valor,
            "score": resultado["score"],
            "aprovado": resultado["aprovado"],
            "nivel_risco": resultado["nivel_risco"]
        }
    )

    return ResultadoAntifraude(
        score=resultado["score"],
        aprovado=resultado["aprovado"],
        motivo=resultado["motivo"],
        regras_ativadas=resultado["regras_ativadas"],
        nivel_risco=resultado["nivel_risco"],
        recomendacao=resultado["recomendacao"]
    )


@router.get("/regras", response_model=dict[str, Any])
def listar_regras() -> dict[str, Any]:
    """
    Exposes the active rule configuration for transparency and auditability.
    """
    regras: list[dict[str, Any]] = [
        {
            "nome": regra.nome,
            "pontos": regra.pontos,
            "descricao": regra.descricao
        }
        for regra in motor_antifraude.regras
    ]

    return {
        "total_regras": len(regras),
        "limite_aprovacao": motor_antifraude.limite_aprovacao,
        "regras": regras
    }
