"""
Business logic for PIX transactions.
Implements idempotency, state machine transitions, and audit logging.
"""
from uuid import uuid4
from typing import Optional, Dict, Any
import re
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.pix.models import TransacaoPix, StatusPix as ModelStatusPix, TipoTransacao
from app.pix.schemas import PixCreateRequest, TipoChavePix
from app.core.logger import logger, audit_log
from app.core.security import mask_sensitive_data
from app.boleto.models import TransacaoBoleto, StatusBoleto
from app.auth.models import User


def get_saldo(db: Session, user_id: str) -> float:
    """Calculates current account balance for a specific user."""
    try:
        total_enviado = db.query(func.sum(TransacaoPix.valor)).filter(
            TransacaoPix.status == ModelStatusPix.CONFIRMADO,
            TransacaoPix.tipo == TipoTransacao.ENVIADO,
            TransacaoPix.user_id == user_id
        ).scalar() or 0.0

        total_recebido = db.query(func.sum(TransacaoPix.valor)).filter(
            TransacaoPix.status == ModelStatusPix.CONFIRMADO,
            TransacaoPix.tipo == TipoTransacao.RECEBIDO,
            TransacaoPix.user_id == user_id
        ).scalar() or 0.0

        total_pago_boleto = db.query(func.sum(TransacaoBoleto.valor)).filter(
            TransacaoBoleto.status == StatusBoleto.PAGO,
            TransacaoBoleto.user_id == user_id
        ).scalar() or 0.0

        return float(total_recebido - total_enviado - total_pago_boleto)
    except Exception as e:
        logger.error(f"Erro ao calcular saldo para user {user_id}: {str(e)}")
        return 0.0


def criar_pix(
    db: Session,
    dados: PixCreateRequest,
    idempotency_key: str,
    correlation_id: str,
    user_id: str,
    tipo: TipoTransacao = TipoTransacao.ENVIADO
) -> TransacaoPix:
    """
    Creates a PIX transaction with strict idempotency guarantees.
    Returns existing transaction if idempotency key collision occurs.
    """
    # Idempotency check
    pix_existente = db.query(TransacaoPix).filter(
        TransacaoPix.idempotency_key == idempotency_key
    ).first()

    if pix_existente:
        logger.info(f"PIX duplicado detectado (idempotência): key={idempotency_key}, id={pix_existente.id}")
        return pix_existente

    # Balance Check for Outgoing Transactions
    if tipo == TipoTransacao.ENVIADO:
        if dados.data_agendamento:
            # Scheduled transaction
            status_inicial = ModelStatusPix.AGENDADO
        else:
            # Immediate transaction - Check Balance
            saldo_atual = get_saldo(db, user_id)
            if dados.valor > saldo_atual:
                raise ValueError("Saldo insuficiente")
            status_inicial = ModelStatusPix.CRIADO
    else:
        # Incoming transaction (Deposit)
        status_inicial = ModelStatusPix.CRIADO

    # Create new transaction
    pix = TransacaoPix(
        id=str(uuid4()),
        valor=dados.valor,
        chave_pix=dados.chave_pix,
        tipo_chave=dados.tipo_chave.value,
        tipo=tipo,
        status=status_inicial,
        idempotency_key=idempotency_key,
        descricao=dados.descricao,
        correlation_id=correlation_id,
        data_agendamento=dados.data_agendamento,
        user_id=user_id
    )

    db.add(pix)
    db.commit()
    db.refresh(pix)

    # Mask sensitive data in logs
    chave_mascarada = mask_sensitive_data(dados.chave_pix)

    audit_log(
        action="pix_criado",
        user=user_id,
        resource=f"pix_id={pix.id}",
        details={
            "correlation_id": correlation_id,
            "valor": dados.valor,
            "chave_mascarada": chave_mascarada,
            "tipo_chave": dados.tipo_chave.value,
            "tipo_transacao": tipo.value,
            "status": status_inicial.value
        }
    )

    logger.info(f"PIX criado: id={pix.id}, valor={dados.valor}, tipo={tipo.value}, status={status_inicial.value}")

    # Real-time Internal Transfer Logic
    # If the destination key belongs to a local user, credit them immediately.
    if tipo == TipoTransacao.ENVIADO and status_inicial != ModelStatusPix.AGENDADO:
        recipient_user = None

        # Search for recipient by Key
        if dados.tipo_chave in [TipoChavePix.CPF, TipoChavePix.CNPJ]:
            clean_key = re.sub(r'\D', '', dados.chave_pix)
            recipient_user = db.query(User).filter(User.cpf_cnpj == clean_key).first()
        elif dados.tipo_chave == TipoChavePix.EMAIL:
            recipient_user = db.query(User).filter(User.email == dados.chave_pix).first()

        if recipient_user:
            # Create incoming transaction for recipient
            pix_recebido = TransacaoPix(
                id=str(uuid4()),
                valor=dados.valor,
                chave_pix=dados.chave_pix,
                tipo_chave=dados.tipo_chave.value,
                tipo=TipoTransacao.RECEBIDO,
                status=ModelStatusPix.CONFIRMADO,
                idempotency_key=f"internal-{idempotency_key}",
                descricao=dados.descricao or "Transferência Recebida",
                correlation_id=correlation_id,
                user_id=recipient_user.id
            )
            db.add(pix_recebido)

            # Apply Credit Limit Increase Rule (50% of received amount)
            aumento_limite = dados.valor * 0.50
            recipient_user.limite_credito += aumento_limite
            db.add(recipient_user)

            logger.info(f"Transferência interna realizada: {dados.valor} para {recipient_user.nome} (ID: {recipient_user.id})")
            logger.info(f"Limite de crédito de {recipient_user.nome} aumentado em R$ {aumento_limite:.2f}")

    return pix


def confirmar_pix(
    db: Session,
    pix_id: str,
    correlation_id: str
) -> Optional[TransacaoPix]:
    """
    Transitions transaction state to CONFIRMED.
    Simulates PSP callback processing.
    """
    pix = db.query(TransacaoPix).filter(TransacaoPix.id == pix_id).first()

    if not pix:
        logger.warning(f"PIX não encontrado para confirmação: id={pix_id}")
        return None

    if pix.status == ModelStatusPix.CONFIRMADO:
        logger.info(f"PIX já confirmado: id={pix_id}")
        return pix

    # Update status
    pix.status = ModelStatusPix.CONFIRMADO
    db.commit()
    db.refresh(pix)

    audit_log(
        action="pix_confirmado",
        user="sistema",
        resource=f"pix_id={pix.id}",
        details={"correlation_id": correlation_id}
    )

    logger.info(f"PIX confirmado: id={pix.id}")

    return pix


def cancelar_pix(
    db: Session,
    pix_id: str,
    user_id: str,
    correlation_id: str
) -> Optional[TransacaoPix]:
    """
    Cancels a scheduled transaction.
    Only allows cancellation if status is AGENDADO.
    """
    pix = db.query(TransacaoPix).filter(
        TransacaoPix.id == pix_id,
        TransacaoPix.user_id == user_id
    ).first()

    if not pix:
        logger.warning(f"Tentativa de cancelamento de PIX inexistente ou de outro usuário: id={pix_id}")
        return None

    if pix.status != ModelStatusPix.AGENDADO:
        raise ValueError("Apenas transações agendadas podem ser canceladas.")

    # Update status
    pix.status = ModelStatusPix.CANCELADO
    db.commit()
    db.refresh(pix)

    audit_log(
        action="pix_cancelado",
        user=user_id,
        resource=f"pix_id={pix.id}",
        details={"correlation_id": correlation_id}
    )

    logger.info(f"PIX cancelado: id={pix.id}")

    return pix


def buscar_pix(db: Session, pix_id: str, user_id: str) -> Optional[TransacaoPix]:
    """Retrieves transaction details by unique identifier and user."""
    return db.query(TransacaoPix).filter(TransacaoPix.id == pix_id, TransacaoPix.user_id == user_id).first()


def listar_extrato(
    db: Session,
    user_id: str,
    limite: int = 50,
    status: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generates transaction ledger with aggregated totals for a specific user.
    """
    query = db.query(TransacaoPix).filter(TransacaoPix.user_id == user_id)

    if status:
        query = query.filter(TransacaoPix.status == status)

    transacoes = query.order_by(TransacaoPix.criado_em.desc()).limit(limite).all()

    # Calculate totals
    total_enviado = db.query(func.sum(TransacaoPix.valor)).filter(
        TransacaoPix.status == ModelStatusPix.CONFIRMADO,
        TransacaoPix.tipo == TipoTransacao.ENVIADO,
        TransacaoPix.user_id == user_id
    ).scalar() or 0

    total_recebido = db.query(func.sum(TransacaoPix.valor)).filter(
        TransacaoPix.status == ModelStatusPix.CONFIRMADO,
        TransacaoPix.tipo == TipoTransacao.RECEBIDO,
        TransacaoPix.user_id == user_id
    ).scalar() or 0

    saldo = total_recebido - total_enviado

    return {
        "total_transacoes": len(transacoes),
        "total_valor": float(total_enviado),  # Keeping for backward compatibility if needed, but saldo is better
        "saldo": float(saldo),
        "transacoes": transacoes
    }
