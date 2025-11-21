"""
FastAPI Router for PIX endpoints.
Exposes RESTful API with strict validation and automated documentation.
"""
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session

from app.pix.models import TipoTransacao
from app.pix.schemas import (
    PixCreateRequest,
    PixConfirmRequest,
    PixResponse,
    PixStatementResponse,
    PixChargeRequest,
    PixChargeResponse,
    StatusPix,
    TipoChavePix
)
from app.pix.service import criar_pix, confirmar_pix, buscar_pix, listar_extrato, cancelar_pix
from app.core.database import get_db
from app.core.logger import get_logger_with_correlation
from app.auth.dependencies import get_current_user, require_active_account
from app.auth.models import User

router = APIRouter(tags=["PIX"])


@router.post("/transacoes", response_model=PixResponse, status_code=201)
def criar_transacao_pix(
    dados: PixCreateRequest,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_active_account),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    **Challenge 2: PIX Transaction API**

    Creates a new transaction with idempotency support.
    **Requires active account (at least one deposit made).**

    - **valor**: Transaction value (R$)
    - **tipo_chave**: Key Type (CPF, EMAIL, TELEFONE, ALEATORIA)
    - **chave_pix**: Valid destination key
    - **X-Idempotency-Key**: Mandatory header to ensure uniqueness

    **Returns:**
    - Transaction metadata and initial state
    """
    # Generate correlation_id for traceability
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        logger.info(f"Iniciando criação de PIX: {dados.model_dump()} para user {current_user.id}")

        pix = criar_pix(
            db,
            dados,
            x_idempotency_key,
            correlation_id,
            user_id=current_user.id,
            tipo=TipoTransacao.ENVIADO
        )

        # Auto-confirm immediate transactions (Simulating instant payment)
        if pix.status == StatusPix.CRIADO and pix.tipo == TipoTransacao.ENVIADO:
            confirmed_pix = confirmar_pix(db, pix.id, correlation_id)
            if confirmed_pix:
                pix = confirmed_pix

        return PixResponse.model_validate(pix)

    except ValueError as e:
        logger.warning(f"Erro de validação PIX: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Erro ao criar PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro interno ao processar PIX")


@router.post("/transacoes/confirmar", response_model=PixResponse)
def confirmar_transacao_pix(
    dados: PixConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Confirms a pending transaction.
    Simulates Payment Service Provider (PSP) callback.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        logger.info(f"Confirmando PIX: {dados.pix_id}")

        # Note: In a real scenario, confirmation might come from a webhook without user context,
        # but for this simulation, we assume the user triggers it or we validate ownership.
        # For now, we just confirm.
        pix = confirmar_pix(db, dados.pix_id, correlation_id)

        if not pix:
            raise HTTPException(status_code=404, detail="Transação não encontrada")

        return PixResponse.model_validate(pix)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao confirmar PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao confirmar transação")


@router.get("/transacoes/{pix_id}", response_model=PixResponse)
def consultar_pix(
    pix_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> PixResponse:
    """
    Retrieves transaction details by ID.
    """
    pix = buscar_pix(db, pix_id, current_user.id)

    if not pix:
        raise HTTPException(status_code=404, detail="Transação não encontrada")

    return PixResponse.model_validate(pix)


@router.delete("/transacoes/{pix_id}", response_model=PixResponse)
def cancelar_agendamento_pix(
    pix_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Cancels a scheduled transaction.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        logger.info(f"Solicitação de cancelamento PIX: {pix_id} user={current_user.id}")

        pix = cancelar_pix(db, pix_id, current_user.id, correlation_id)

        if not pix:
            raise HTTPException(status_code=404, detail="Transação não encontrada ou não pertence ao usuário")

        return PixResponse.model_validate(pix)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Erro ao cancelar PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao cancelar transação")


@router.get("/extrato", response_model=PixStatementResponse)
def consultar_extrato(
    status: Optional[StatusPix] = None,
    limite: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> PixStatementResponse:
    """
    Retrieves transaction ledger with optional status filtering.
    """
    resultado: Dict[str, Any] = listar_extrato(db, current_user.id, limite, status.value if status else None)

    return PixStatementResponse(
        total_transacoes=resultado["total_transacoes"],
        total_valor=resultado["total_valor"],
        saldo=resultado["saldo"],
        transacoes=[PixResponse.model_validate(t) for t in resultado["transacoes"]]
    )


@router.post("/cobrar", response_model=PixChargeResponse)
def gerar_cobranca_pix(
    dados: PixChargeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixChargeResponse:
    """
    Generates a PIX Charge (Receive Money).
    Returns a simulated Copy & Paste code and QR Code URL.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(f"Gerando cobrança Pix: valor={dados.valor} para user {current_user.id}")

    # Simulate a Pix Copy & Paste string (EMV standard-ish mock)
    # In a real app, this would be generated by a library like 'pix-qrcode'
    mock_payload = (
        f"00020126580014BR.GOV.BCB.PIX0136123e4567-e89b-12d3-a456-426614174000520400005303986540"
        f"{str(dados.valor).replace('.', '')}5802BR5913NewCredit User6008BRASILIA62070503***6304"
    )

    # Generate simulation URL for the QR Code
    # This allows the user to scan the QR code with a camera and open the simulation page
    base_url = str(request.base_url).rstrip('/')
    simulation_url = f"{base_url}/pix/pagar-qrcode?valor={dados.valor}&desc={dados.descricao or ''}"

    # Using a public API to generate QR Code image that points to the simulation URL
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={simulation_url}"

    return PixChargeResponse(
        valor=dados.valor,
        descricao=dados.descricao,
        copia_e_cola=mock_payload,
        qr_code_url=qr_url
    )


@router.post("/receber/confirmar", response_model=PixResponse)
def processar_recebimento_pix(
    dados: PixChargeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Processes a received PIX (Deposit).
    Called by the simulation page when the user confirms payment.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(f"Processando recebimento PIX: valor={dados.valor} para user {current_user.id}")

    # Create a transaction representing the deposit
    # We use a dummy key for the sender since it's a simulation
    pix_dados = PixCreateRequest(
        valor=dados.valor,
        chave_pix="SIMULACAO_QR_CODE",
        tipo_chave=TipoChavePix.ALEATORIA,
        descricao=dados.descricao or "Depósito via QR Code"
    )

    # Generate a unique idempotency key
    idempotency_key = f"deposito-{uuid4()}"

    try:
        pix = criar_pix(db, pix_dados, idempotency_key, correlation_id, user_id=current_user.id, tipo=TipoTransacao.RECEBIDO)

        # Auto-confirm since it's a simulation
        confirmar_pix(db, pix.id, correlation_id)

        # Increase credit limit by 50% of the deposited amount
        aumento_limite = dados.valor * 0.50
        current_user.limite_credito += aumento_limite
        db.add(current_user)
        db.commit()
        db.refresh(current_user)

        logger.info(f"Limite de crédito aumentado em R$ {aumento_limite:.2f} para user {current_user.id}")

        return PixResponse.model_validate(pix)

    except Exception as e:
        logger.error(f"Erro ao processar recebimento: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao processar depósito")
