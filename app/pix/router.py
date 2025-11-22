"""
FastAPI Router for PIX endpoints.
Exposes RESTful API with strict validation and automated documentation.
"""
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session

from app.pix.models import TransactionType, PixTransaction
from app.pix.schemas import (
    PixCreateRequest,
    PixConfirmRequest,
    PixResponse,
    PixStatementResponse,
    PixChargeRequest,
    PixChargeResponse,
    PixChargeConfirmRequest,
    PixStatus,
    PixKeyType
)
from app.pix.service import create_pix, confirm_pix, get_pix, list_statement, cancel_pix
from app.core.database import get_db
from app.core.logger import get_logger_with_correlation
from app.auth.dependencies import get_current_user, require_active_account
from app.auth.models import User
from app.core.utils import mask_cpf_cnpj, format_brasilia_time

router = APIRouter(tags=["PIX"])


@router.post("/transacoes", response_model=PixResponse, status_code=201)
def create_pix_transaction(
    data: PixCreateRequest,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_active_account),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    **Challenge 2: PIX Transaction API**

    Creates a new transaction with idempotency support.
    **Requires active account (at least one deposit made).**

    - **value**: Transaction value (R$)
    - **key_type**: Key Type (CPF, EMAIL, PHONE, RANDOM)
    - **pix_key**: Valid destination key
    - **X-Idempotency-Key**: Mandatory header to ensure uniqueness

    **Returns:**
    - Transaction metadata and initial state
    """
    # Generate correlation_id for traceability
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        logger.info(f"Starting PIX creation: {data.model_dump()} for user {current_user.id}")

        pix = create_pix(
            db,
            data,
            x_idempotency_key,
            correlation_id,
            user_id=current_user.id,
            type=TransactionType.SENT
        )

        # Auto-confirm immediate transactions (Simulating instant payment)
        if pix.status == PixStatus.CREATED and pix.type == TransactionType.SENT:
            confirmed_pix = confirm_pix(db, pix.id, correlation_id)
            if confirmed_pix:
                pix = confirmed_pix

        return build_pix_response(pix, db)

    except ValueError as e:
        logger.warning(f"PIX validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error processing PIX")


@router.post("/transacoes/confirmar", response_model=PixResponse)
def confirm_pix_transaction(
    data: PixConfirmRequest,
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
        logger.info(f"Confirming PIX: {data.pix_id}")

        # Note: In a real scenario, confirmation might come from a webhook without user context,
        # but for this simulation, we assume the user triggers it or we validate ownership.
        # For now, we just confirm.
        pix = confirm_pix(db, data.pix_id, correlation_id)

        if not pix:
            raise HTTPException(status_code=404, detail="Transaction not found")

        return build_pix_response(pix, db)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error confirming PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error confirming transaction")


@router.get("/transacoes/{pix_id}", response_model=PixResponse)
def get_pix_transaction(
    pix_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> PixResponse:
    """
    Retrieves transaction details by ID.
    """
    pix = get_pix(db, pix_id, current_user.id)

    if not pix:
        raise HTTPException(status_code=404, detail="Transaction not found")

    return build_pix_response(pix, db)


@router.delete("/transacoes/{pix_id}", response_model=PixResponse)
def cancel_pix_scheduling(
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
        logger.info(f"PIX cancellation request: {pix_id} user={current_user.id}")

        pix = cancel_pix(db, pix_id, current_user.id, correlation_id)

        if not pix:
            raise HTTPException(status_code=404, detail="Transaction not found or does not belong to user")

        return build_pix_response(pix, db)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error cancelling PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error cancelling transaction")


@router.get("/extrato", response_model=PixStatementResponse)
def get_statement(
    status: Optional[PixStatus] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> PixStatementResponse:
    """
    Retrieves transaction ledger with optional status filtering.
    """
    result: Dict[str, Any] = list_statement(db, current_user.id, limit, status.value if status else None)

    return PixStatementResponse(
        total_transactions=result["total_transactions"],
        total_value=result["total_value"],
        balance=result["balance"],
        transactions=[build_pix_response(t, db) for t in result["transactions"]]
    )


@router.post("/cobrar", response_model=PixChargeResponse)
def generate_pix_charge(
    data: PixChargeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixChargeResponse:
    """
    Generates a PIX Charge (Receive Money).
    Creates a pending transaction that expires after one use.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(f"Generating PIX charge: value={data.value} for user {current_user.id}")

    # Create a pending transaction for this charge
    # This ensures the charge is stateful and can be expired/validated
    charge_id = str(uuid4())

    pix_data = PixCreateRequest(
        value=data.value,
        pix_key="DYNAMIC_QR_CODE",
        key_type=PixKeyType.RANDOM,
        description=data.description or "Cobrança via QR Code"
    )

    # Create transaction with CREATED status (Pending Payment)
    # We use the charge_id as the transaction ID
    pix = PixTransaction(
        id=charge_id,
        value=data.value,
        pix_key=pix_data.pix_key,
        key_type=pix_data.key_type.value,
        type=TransactionType.RECEIVED,
        status=PixStatus.CREATED,
        idempotency_key=f"charge-{charge_id}",
        description=pix_data.description,
        correlation_id=correlation_id,
        user_id=current_user.id
    )

    db.add(pix)
    db.commit()
    db.refresh(pix)

    # Simulate a Pix Copy & Paste string
    mock_payload = (
        f"00020126580014BR.GOV.BCB.PIX0136{charge_id}520400005303986540"
        f"{str(data.value).replace('.', '')}5802BR5913NewCredit User6008BRASILIA62070503***6304"
    )

    # Generate simulation URL with the CHARGE ID
    base_url = str(request.base_url).rstrip('/')
    simulation_url = f"{base_url}/pix/pagar-qrcode?id={charge_id}"

    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={simulation_url}"

    return PixChargeResponse(
        charge_id=charge_id,
        value=data.value,
        description=data.description,
        copy_and_paste=mock_payload,
        qr_code_url=qr_url
    )


@router.post("/receber/confirmar", response_model=PixResponse)
def process_pix_receipt(
    data: PixChargeConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Processes a received PIX (Deposit) for a specific Charge ID.
    Enforces One-Time Use: If charge is already paid, rejects.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(f"Processing PIX receipt for charge: {data.charge_id}")

    # Find the charge transaction
    # Note: In a real scenario, we would verify if the payer is paying the right person.
    # Here, we assume the current_user is the one triggering the simulation (the payer/payee context is simplified).
    # Actually, usually the PAYER triggers this. But in this simulation, the RECEIVER (User) might be opening the link?
    # No, the link is opened by the Payer.
    # Let's assume the transaction exists in the DB.

    pix = db.query(PixTransaction).filter(PixTransaction.id == data.charge_id).first()

    if not pix:
        raise HTTPException(status_code=404, detail="Cobrança não encontrada.")

    # CRITICAL: One-Time Use Check
    if pix.status == PixStatus.CONFIRMED:
        logger.warning(f"Attempt to reuse paid charge: {data.charge_id}")
        raise HTTPException(status_code=409, detail="Esta cobrança já foi paga e não pode ser utilizada novamente.")

    if pix.status != PixStatus.CREATED:
        raise HTTPException(status_code=400, detail=f"Status da cobrança inválido: {pix.status}")

    try:
        # Confirm the transaction
        pix.status = PixStatus.CONFIRMED
        db.add(pix)

        # Credit the receiver (User who created the charge)
        receiver_user = db.query(User).filter(User.id == pix.user_id).first()
        if receiver_user:
            # Increase credit limit logic
            limit_increase = pix.value * 0.50
            receiver_user.credit_limit += limit_increase
            db.add(receiver_user)
            logger.info(f"Credit limit increased by R$ {limit_increase:.2f} for user {receiver_user.id}")

        db.commit()
        db.refresh(pix)

        return build_pix_response(pix, db)

    except Exception as e:
        db.rollback()
        logger.error(f"Error processing receipt: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error processing deposit")


def build_pix_response(pix: Any, db: Session) -> PixResponse:
    """
    Constructs a PixResponse with enriched data (names, masked docs, formatted time).
    """
    # 2. Identify Sender and Receiver
    # Default values
    sender_name = "Unknown"
    sender_doc = "***"
    receiver_name = "Unknown"
    receiver_doc = "***"

    # Fetch the owner of this transaction record
    owner_user = db.query(User).filter(User.id == pix.user_id).first()

    if pix.type == TransactionType.SENT:
        # The owner is the sender
        if owner_user:
            sender_name = owner_user.name
            sender_doc = mask_cpf_cnpj(owner_user.cpf_cnpj)

        # Try to find the receiver via correlation_id (Internal Transfer)
        # Look for a RECEIVED transaction with same correlation_id
        receiver_tx = db.query(PixTransaction).filter(
            PixTransaction.correlation_id == pix.correlation_id,
            PixTransaction.type == TransactionType.RECEIVED
        ).first()

        if receiver_tx:
            receiver_user = db.query(User).filter(User.id == receiver_tx.user_id).first()
            if receiver_user:
                receiver_name = receiver_user.name
                receiver_doc = mask_cpf_cnpj(receiver_user.cpf_cnpj)
        else:
            # External or not found - Try to resolve from Key
            # If key is CPF/CNPJ, we might mask it. If it's email, show it.
            receiver_name = "External Receiver"
            receiver_doc = mask_cpf_cnpj(pix.pix_key) # Best effort

    elif pix.type == TransactionType.RECEIVED:
        # The owner is the receiver
        if owner_user:
            receiver_name = owner_user.name
            receiver_doc = mask_cpf_cnpj(owner_user.cpf_cnpj)

        # Try to find the sender via correlation_id (Internal Transfer)
        # Look for an SENT transaction with same correlation_id
        sender_tx = db.query(PixTransaction).filter(
            PixTransaction.correlation_id == pix.correlation_id,
            PixTransaction.type == TransactionType.SENT
        ).first()

        if sender_tx:
            sender_user = db.query(User).filter(User.id == sender_tx.user_id).first()
            if sender_user:
                sender_name = sender_user.name
                sender_doc = mask_cpf_cnpj(sender_user.cpf_cnpj)
        else:
            # Deposit or External
            if "SIMULACAO" in pix.pix_key or "Deposit" in (pix.description or ""):
                sender_name = "Deposit via QR Code"
                sender_doc = "Financial Institution"
            else:
                sender_name = "External Sender"
                sender_doc = "***"

    return PixResponse(
        id=pix.id,
        value=pix.value,
        pix_key=pix.pix_key,
        key_type=pix.key_type,
        type=pix.type,
        status=pix.status,
        description=pix.description,
        scheduled_date=pix.scheduled_date,
        created_at=pix.created_at,
        updated_at=pix.updated_at,
        formatted_time=format_brasilia_time(pix.created_at),
        sender_name=sender_name,
        sender_doc=sender_doc,
        receiver_name=receiver_name,
        receiver_doc=receiver_doc
    )
