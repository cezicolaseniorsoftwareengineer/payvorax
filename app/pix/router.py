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
    PixQrCodePayRequest,
    PixStatus,
    PixKeyType
)
from app.pix.service import create_pix, confirm_pix, get_pix, list_statement, cancel_pix, ensure_asaas_customer
from app.pix.internal_transfer import find_recipient_user
from app.adapters.gateway_factory import get_payment_gateway
from decimal import Decimal
from app.core.database import get_db
from app.core.logger import get_logger_with_correlation, audit_log
from app.auth.dependencies import get_current_user, require_active_account
from app.auth.models import User
from app.core.utils import mask_cpf_cnpj, format_brasilia_time

router = APIRouter(tags=["PIX"])


@router.post("/transacoes", response_model=PixResponse, status_code=201)
def create_pix_transaction(
    data: PixCreateRequest,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    **Challenge 2: PIX Transaction API**

    Creates a new transaction with idempotency support.
    **Requires active account (at least one deposit made) for outgoing transfers, except for self-deposits.**

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

    # Enforce Active Account Policy manually, but allow Self-Deposit (Copia e Cola)
    # This allows new users to fund their account via "Pix Copia e Cola" of their own charge.
    has_deposit = db.query(PixTransaction).filter(
        PixTransaction.user_id == current_user.id,
        PixTransaction.type == TransactionType.RECEIVED,
        PixTransaction.status == PixStatus.CONFIRMED
    ).first()

    if not has_deposit:
        # If no deposit, only allow if it looks like a Copia e Cola (potential self-deposit)
        # The service layer will validate if it is indeed a self-deposit and handle it.
        # If it is NOT a self-deposit, the service will check balance (which is 0) and fail safely.
        if not (data.key_type == PixKeyType.RANDOM and len(data.pix_key) > 36):
             raise HTTPException(
                status_code=403,
                detail="Inactive account. Make a first deposit (Received PIX) to unlock all features."
            )

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
    Optimized with batch loading to prevent N+1 query issues.
    """
    result: Dict[str, Any] = list_statement(db, current_user.id, limit, status.value if status else None)

    transactions = result["transactions"]

    # --- BATCH LOADING OPTIMIZATION ---
    # 1. Collect all relevant IDs
    user_ids = {t.user_id for t in transactions}
    correlation_ids = {t.correlation_id for t in transactions if t.correlation_id}

    # 2. Fetch all related Users in one query
    # We need the current user (already have) and potentially others if we were admin,
    # but here we mostly need the current user.
    # However, for internal transfers, we need the OTHER user.
    # The 'build_pix_response' logic looks for related transactions to find the other user.

    # 3. Fetch all related Transactions (Counterparts) in one query
    related_txs = []
    if correlation_ids:
        related_txs = db.query(PixTransaction).filter(
            PixTransaction.correlation_id.in_(correlation_ids),
            PixTransaction.id.notin_([t.id for t in transactions]) # Exclude self
        ).all()

    # Map correlation_id -> related_transaction
    related_tx_map = {tx.correlation_id: tx for tx in related_txs}

    # 4. Collect User IDs from related transactions
    related_user_ids = {tx.user_id for tx in related_txs}
    all_user_ids = user_ids.union(related_user_ids)

    # 5. Fetch all Users in one query
    users = db.query(User).filter(User.id.in_(all_user_ids)).all()
    user_map = {u.id: u for u in users}

    # 6. Build Responses in Memory
    response_list = []

    for pix in transactions:
        # Default values
        sender_name = "Unknown"
        sender_doc = "***"
        receiver_name = "Unknown"
        receiver_doc = "***"

        owner_user = user_map.get(pix.user_id)

        if pix.type == TransactionType.SENT:
            # Owner is Sender
            if owner_user:
                sender_name = owner_user.name
                sender_doc = mask_cpf_cnpj(owner_user.cpf_cnpj)

            # Find Receiver (Counterpart)
            receiver_tx = related_tx_map.get(pix.correlation_id)
            # Ensure it's the right type (RECEIVED)
            if receiver_tx and receiver_tx.type == TransactionType.RECEIVED:
                receiver_user = user_map.get(receiver_tx.user_id)
                if receiver_user:
                    receiver_name = receiver_user.name
                    receiver_doc = mask_cpf_cnpj(receiver_user.cpf_cnpj)
            else:
                # External
                receiver_name = "External Receiver"
                receiver_doc = mask_cpf_cnpj(pix.pix_key)

        elif pix.type == TransactionType.RECEIVED:
            # Owner is Receiver
            if owner_user:
                receiver_name = owner_user.name
                receiver_doc = mask_cpf_cnpj(owner_user.cpf_cnpj)

            # Find Sender (Counterpart)
            sender_tx = related_tx_map.get(pix.correlation_id)
            # Ensure it's the right type (SENT)
            if sender_tx and sender_tx.type == TransactionType.SENT:
                sender_user = user_map.get(sender_tx.user_id)
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

        response_list.append(PixResponse(
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
        ))

    return PixStatementResponse(
        total_transactions=result["total_transactions"],
        total_value=result["total_value"],
        balance=result["balance"],
        transactions=response_list
    )


def _normalize_pix_key(chave: str, tipo: str) -> str:
    """
    Normalizes a PIX key to the format expected by Asaas before sending to the API.

    Rules:
    - TELEFONE: strip all non-digits, ensure E.164 format (+55DDDNNNNNNNNN)
    - CPF: strip all non-digits (11 digits)
    - CNPJ: strip all non-digits (14 digits)
    - EMAIL: lowercase and strip whitespace
    - ALEATORIA / EVP: strip whitespace only
    """
    import re as _re

    if tipo in ("TELEFONE", "PHONE"):
        digits = _re.sub(r"\D", "", chave)
        # Remove leading country code if already present (55...)
        if digits.startswith("55") and len(digits) > 11:
            digits = digits[2:]
        # digits should now be DDD + number (10 or 11 digits)
        return f"+55{digits}"

    if tipo in ("CPF", "CNPJ"):
        return _re.sub(r"\D", "", chave)

    if tipo == "EMAIL":
        return chave.strip().lower()

    # ALEATORIA / EVP / unknown — trim only
    return chave.strip()


@router.get("/consultar-chave", response_model=Dict[str, Any])
def lookup_pix_key_endpoint(
    chave: str,
    tipo: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Validates a PIX key and returns beneficiary (recipient) information.
    Priority: internal PayvoraX users -> Asaas gateway -> key format valid.
    The key is normalized before any lookup to ensure correct format for Asaas.
    """
    import re as _re

    # 1. Resolve key type enum
    from app.pix.schemas import PixKeyType as _PKT
    try:
        key_type_enum = _PKT(tipo)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Tipo de chave invalido: {tipo}")

    # 2. Normalize the raw key to its canonical form
    chave_normalizada = _normalize_pix_key(chave.strip(), tipo)

    # 3. Check internal PayvoraX users first (use original for email, normalized for cpf/phone)
    recipient = find_recipient_user(db, chave_normalizada, key_type_enum)
    if not recipient and chave_normalizada != chave.strip():
        # Fallback: try with raw value in case internal store uses different format
        recipient = find_recipient_user(db, chave.strip(), key_type_enum)

    if recipient:
        return {
            "found": True,
            "name": recipient.name,
            "document": mask_cpf_cnpj(recipient.cpf_cnpj),
            "bank": "Bio Code Tech Pay",
            "internal": True,
        }

    # 4. Try gateway lookup with normalized key
    gateway = get_payment_gateway()
    if gateway:
        try:
            info = gateway.lookup_pix_key(chave_normalizada, tipo)
            if info and info.get("name"):
                return {
                    "found": True,
                    "name": info["name"],
                    "document": info.get("document", "***"),
                    "bank": info.get("bank", "Rede Bancaria"),
                    "internal": False,
                }
        except Exception:
            pass  # Gateway lookup is best-effort

    # 5. Key not found in any source — return not found so UI can warn the user
    raise HTTPException(status_code=404, detail="Chave Pix nao encontrada. Verifique os dados e tente novamente.")


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
    Attempts real Asaas charge first; falls back to local simulation.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(f"Generating PIX charge: value={data.value} for user {current_user.id}")

    description = data.description or "Bio Code Tech Pay - Cobranca PIX"

    ASAAS_MIN_VALUE = Decimal("5.00")

    # --- Attempt real Asaas charge (minimum R$5.00 required by Asaas production) ---
    gateway = get_payment_gateway()
    if gateway and Decimal(str(data.value)) >= ASAAS_MIN_VALUE:
        try:
            customer_id = ensure_asaas_customer(db, current_user.id)
            if customer_id:
                charge_data = gateway.create_pix_charge(
                    value=Decimal(str(data.value)),
                    description=description,
                    customer_id=customer_id,
                    idempotency_key=f"cobrar-{correlation_id}"
                )

                # Store transaction with Asaas payment ID
                pix = PixTransaction(
                    id=charge_data["charge_id"],
                    value=data.value,
                    pix_key=charge_data.get("qr_code", ""),
                    key_type=PixKeyType.RANDOM.value,
                    type=TransactionType.RECEIVED,
                    status=PixStatus.CREATED,
                    idempotency_key=f"cobrar-{correlation_id}",
                    description=description,
                    correlation_id=correlation_id,
                    user_id=current_user.id
                )
                db.add(pix)
                db.commit()
                db.refresh(pix)

                # Asaas returns base64 image — prefix for data URI
                raw_image = charge_data.get("qr_code_url", "")
                if raw_image and not raw_image.startswith("data:"):
                    qr_url = f"data:image/png;base64,{raw_image}"
                else:
                    qr_url = raw_image

                logger.info(f"Real Asaas charge created: {pix.id}")
                return PixChargeResponse(
                    charge_id=pix.id,
                    value=data.value,
                    description=description,
                    copy_and_paste=charge_data.get("qr_code", ""),
                    qr_code_url=qr_url,
                    is_real_charge=True
                )
        except Exception as e:
            logger.warning(f"Asaas charge failed, falling back to simulation: {str(e)}")
    elif gateway and Decimal(str(data.value)) < ASAAS_MIN_VALUE:
        logger.info(
            f"Value R${data.value:.2f} is below Asaas minimum R$5.00 — using local simulation."
        )

    # --- Fallback: local simulation ---
    logger.info(f"Creating local simulation charge for user {current_user.id}")
    charge_id = str(uuid4())

    pix = PixTransaction(
        id=charge_id,
        value=data.value,
        pix_key="DYNAMIC_QR_CODE",
        key_type=PixKeyType.RANDOM.value,
        type=TransactionType.RECEIVED,
        status=PixStatus.CREATED,
        idempotency_key=f"charge-{charge_id}",
        description=description,
        correlation_id=correlation_id,
        user_id=current_user.id
    )

    db.add(pix)
    db.commit()
    db.refresh(pix)

    mock_payload = (
        f"00020126580014BR.GOV.BCB.PIX0136{charge_id}520400005303986540"
        f"{str(data.value).replace('.', '')}5802BR5921Bio Code Tech Pay6008BRASILIA62070503***6304"
    )

    base_url = str(request.base_url).rstrip('/')
    simulation_url = f"{base_url}/pix/pagar-qrcode?id={charge_id}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={simulation_url}"

    return PixChargeResponse(
        charge_id=charge_id,
        value=data.value,
        description=description,
        copy_and_paste=mock_payload,
        qr_code_url=qr_url,
        is_real_charge=False
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
    pix = db.query(PixTransaction).filter(PixTransaction.id == data.charge_id).first()

    if not pix:
        logger.error(f"Charge not found: {data.charge_id}")
        raise HTTPException(status_code=404, detail="Cobrança não encontrada.")

    logger.info(f"Charge found: {pix.id}, Status: {pix.status}, Value: {pix.value}")

    # CRITICAL: One-Time Use Check
    if pix.status == PixStatus.CONFIRMED:
        logger.warning(f"Attempt to reuse paid charge: {data.charge_id}")
        raise HTTPException(status_code=409, detail="Esta cobrança já foi paga e não pode ser utilizada novamente.")

    if pix.status != PixStatus.CREATED:
        logger.error(f"Invalid charge status: {pix.status} for charge {data.charge_id}")
        raise HTTPException(status_code=400, detail=f"Status da cobrança inválido: {pix.status}")

    try:
        # Confirm the transaction
        pix.status = PixStatus.CONFIRMED
        db.add(pix)

        # Credit the receiver balance (User who created the charge)
        receiver_user = db.query(User).filter(User.id == pix.user_id).first()
        if receiver_user:
            previous_balance = receiver_user.balance
            receiver_user.balance += pix.value
            limit_increase = pix.value * 0.50
            receiver_user.credit_limit += limit_increase
            db.add(receiver_user)
            logger.info(
                f"Deposit confirmed: user={receiver_user.id}, "
                f"amount=R${pix.value:.2f}, "
                f"balance: R${previous_balance:.2f} -> R${receiver_user.balance:.2f}"
            )
        else:
            logger.warning(f"Receiver user not found for charge {pix.id} (User ID: {pix.user_id})")

        db.commit()
        db.refresh(pix)

        logger.info(f"Charge {pix.id} successfully confirmed.")
        return build_pix_response(pix, db)

    except Exception as e:
        db.rollback()
        logger.error(f"Error processing receipt: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error processing deposit")


@router.post("/cobrar/{charge_id}/verificar", response_model=PixResponse)
def verify_pix_charge_payment(
    charge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Verifies if a real Asaas PIX charge has been paid.
    When confirmed by Asaas, credits user.balance and marks transaction CONFIRMED.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    pix = db.query(PixTransaction).filter(
        PixTransaction.id == charge_id,
        PixTransaction.user_id == current_user.id
    ).first()

    if not pix:
        raise HTTPException(status_code=404, detail="Cobrança não encontrada.")

    if pix.status == PixStatus.CONFIRMED:
        return build_pix_response(pix, db)

    if pix.status != PixStatus.CREATED:
        raise HTTPException(status_code=400, detail=f"Status inválido: {pix.status}")

    gateway = get_payment_gateway()
    if not gateway:
        raise HTTPException(status_code=503, detail="Servico de pagamento temporariamente indisponivel.")

    try:
        charge_status = gateway.get_charge_status(charge_id)
        logger.info(f"Asaas charge status: {charge_id} -> {charge_status.get('status')}")

        if charge_status.get("status") == "CONFIRMED":
            pix.status = PixStatus.CONFIRMED
            db.add(pix)

            receiver_user = db.query(User).filter(User.id == pix.user_id).first()
            if receiver_user:
                previous_balance = receiver_user.balance
                receiver_user.balance += pix.value
                receiver_user.credit_limit += pix.value * 0.50
                db.add(receiver_user)
                logger.info(
                    f"Asaas deposit confirmed: user={receiver_user.id}, "
                    f"amount=R${pix.value:.2f}, "
                    f"balance: R${previous_balance:.2f} -> R${receiver_user.balance:.2f}"
                )

            db.commit()
            db.refresh(pix)
            return build_pix_response(pix, db)

        # Not paid yet
        raise HTTPException(
            status_code=202,
            detail=f"Pagamento ainda nao confirmado. Status: {charge_status.get('status', 'PENDING')}. Aguarde e tente novamente."
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error verifying Asaas charge {charge_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao verificar o pagamento. Tente novamente.")


@router.post("/qrcode/pagar", response_model=Dict[str, Any], status_code=200)
def pay_pix_qrcode(
    data: PixQrCodePayRequest,
    x_idempotency_key: str = Header(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> Dict[str, Any]:
    """
    Pays a PIX QR Code (scanned or Pix Copia e Cola EMV payload).

    Routing logic:
    1. If the EMV payload contains an internal Bio Code charge UUID -> confirm locally.
    2. Otherwise -> dispatch to Asaas POST /pix/qrCodes/pay.

    - **payload**: Full EMV string (000201...) or Pix Copia e Cola code
    - **description**: Optional description (max 140 chars)
    """
    import re as _re

    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(
        f"QR Code payment request: user={current_user.id}, "
        f"payload_length={len(data.payload)}, idempotency={x_idempotency_key}"
    )

    idempotency_key = x_idempotency_key or str(uuid4())

    # Idempotency guard: reject duplicate payment attempts
    if x_idempotency_key:
        existing = db.query(PixTransaction).filter(
            PixTransaction.idempotency_key == x_idempotency_key
        ).first()
        if existing:
            logger.info(f"Duplicate QR Code payment blocked: idempotency_key={x_idempotency_key}")
            return build_pix_response(existing, db).model_dump()

    sender = db.query(User).filter(User.id == current_user.id).first()
    if not sender:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")

    # -------------------------------------------------------------------------
    # Routing 1: detect internal Bio Code charge in the payload via two methods:
    #   a) UUID regex — simulation EMVs embed the charge UUID (e.g. "0136{uuid}")
    #   b) pix_key exact match — real Asaas charges store copy-paste EMV as pix_key
    # -------------------------------------------------------------------------
    UUID_RE = _re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        _re.IGNORECASE
    )

    internal_charge = None

    # 1a: UUID scan (simulation charges)
    for candidate_id in UUID_RE.findall(data.payload):
        charge = db.query(PixTransaction).filter(
            PixTransaction.id == candidate_id,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.status == PixStatus.CREATED
        ).first()
        if charge:
            internal_charge = charge
            break

    # 1b: pix_key exact match (real Asaas charges — pix_key column is VARCHAR(200))
    if not internal_charge:
        pix_key_lookup = data.payload[:200]
        internal_charge = db.query(PixTransaction).filter(
            PixTransaction.pix_key == pix_key_lookup,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.status == PixStatus.CREATED
        ).first()

    if internal_charge:
        charge_value = float(internal_charge.value)
        receiver = db.query(User).filter(User.id == internal_charge.user_id).first()
        if not receiver:
            raise HTTPException(status_code=422, detail="Recebedor da cobrança não encontrado.")

        logger.info(
            f"Internal charge detected: charge_id={internal_charge.id}, "
            f"payer={current_user.id}, receiver={receiver.id}, value={charge_value}"
        )

        if internal_charge.status != PixStatus.CREATED:
            raise HTTPException(status_code=409, detail="Esta cobranca ja foi paga.")

        is_self_deposit = (internal_charge.user_id == current_user.id)

        if not is_self_deposit:
            if sender.balance < charge_value:
                raise HTTPException(
                    status_code=400,
                    detail=f"Saldo insuficiente. Disponivel: R$ {sender.balance:.2f}, Necessario: R$ {charge_value:.2f}"
                )
            previous_balance = sender.balance
            sender.balance -= charge_value
            db.add(sender)
            logger.info(
                f"Internal QR payment: debited payer={sender.id}, "
                f"amount=R${charge_value:.2f}, "
                f"balance: R${previous_balance:.2f} -> R${sender.balance:.2f}"
            )

        internal_charge.status = PixStatus.CONFIRMED
        db.add(internal_charge)
        receiver.balance += charge_value
        receiver.credit_limit += charge_value * 0.50
        db.add(receiver)

        if not is_self_deposit:
            sent_pix = PixTransaction(
                id=str(uuid4()),
                value=float(charge_value),
                pix_key=internal_charge.pix_key,
                key_type=PixKeyType.RANDOM.value,
                type=TransactionType.SENT,
                status=PixStatus.CONFIRMED,
                idempotency_key=idempotency_key,
                description=data.description or "PIX QR Code Payment",
                correlation_id=internal_charge.correlation_id,
                user_id=current_user.id
            )
            db.add(sent_pix)
            db.commit()
            db.refresh(sent_pix)
            audit_log(
                action="PIX_QRCODE_INTERNAL_PAYMENT",
                user_id=str(current_user.id),
                details={
                    "charge_id": internal_charge.id,
                    "value": float(charge_value),
                    "receiver_id": str(receiver.id)
                }
            )
            result_dict = build_pix_response(sent_pix, db).model_dump()
            result_dict["receiver_name"] = receiver.name
            return result_dict
        else:
            db.commit()
            db.refresh(internal_charge)
            return build_pix_response(internal_charge, db).model_dump()

    # -------------------------------------------------------------------------
    # Routing 2: no internal charge found — dispatch to Asaas.
    # -------------------------------------------------------------------------
    if sender.balance <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Saldo insuficiente. Disponivel: R$ {sender.balance:.2f}"
        )

    gateway = get_payment_gateway()
    if not gateway:
        raise HTTPException(
            status_code=503,
            detail="Servico de pagamento temporariamente indisponivel."
        )

    try:
        result = gateway.pay_qr_code(
            payload=data.payload,
            description=data.description or "Bio Code Tech Pay QR Code Payment",
            idempotency_key=idempotency_key
        )
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Asaas QR Code payment failed: {error_msg}", exc_info=True)
        try:
            import json as _json
            detail_raw = getattr(e, 'response', None)
            if detail_raw is not None:
                body = _json.loads(detail_raw.text)
                errors = body.get("errors", [])
                if errors:
                    error_msg = "; ".join(
                        err.get("description") or err.get("code", "erro desconhecido")
                        for err in errors
                    )
        except Exception:
            pass
        raise HTTPException(status_code=422, detail=error_msg)

    payment_value = float(result.get("value") or 0)

    if payment_value > 0 and sender.balance < payment_value:
        raise HTTPException(
            status_code=400,
            detail=f"Saldo insuficiente. Disponivel: R$ {sender.balance:.2f}, Necessario: R$ {payment_value:.2f}"
        )

    payment_id = result.get("payment_id") or str(uuid4())
    asaas_status = result.get("status", "BANK_PROCESSING")
    pix_status = PixStatus.CONFIRMED if asaas_status == "CONFIRMED" else PixStatus.SENT
    pix_key_ref = data.payload[:197] + "..." if len(data.payload) > 200 else data.payload

    pix = PixTransaction(
        id=payment_id,
        value=payment_value if payment_value > 0 else 0.01,
        pix_key=pix_key_ref,
        key_type=PixKeyType.RANDOM.value,
        type=TransactionType.SENT,
        status=pix_status,
        idempotency_key=idempotency_key,
        description=data.description or "PIX QR Code Payment",
        correlation_id=result.get("end_to_end_id") or correlation_id,
        user_id=current_user.id
    )
    db.add(pix)

    if payment_value > 0:
        previous_balance = sender.balance
        sender.balance -= payment_value
        db.add(sender)
        logger.info(
            f"QR Code payment dispatched: id={payment_id}, user={sender.id}, "
            f"amount=R${payment_value:.2f}, "
            f"balance: R${previous_balance:.2f} -> R${sender.balance:.2f}"
        )

    db.commit()
    db.refresh(pix)

    audit_log(
        action="PIX_QRCODE_PAYMENT",
        user_id=str(current_user.id),
        details={
            "payment_id": payment_id,
            "value": payment_value,
            "status": asaas_status,
            "receiver_name": result.get("receiver_name", "")
        }
    )

    result_dict = build_pix_response(pix, db).model_dump()
    if result.get("receiver_name"):
        result_dict["receiver_name"] = result["receiver_name"]
    return result_dict


@router.post("/webhook/asaas", status_code=200)
async def asaas_webhook(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
    x_correlation_id: str = Header(default=None)
):
    """
    Asaas payment webhook receiver.
    Auto-confirms charges when Asaas notifies PAYMENT_RECEIVED or PAYMENT_CONFIRMED.
    Configure in Asaas dashboard: Settings > Integrations > Webhooks > URL: /pix/webhook/asaas
    Token must match ASAAS_WEBHOOK_TOKEN environment variable.
    """
    from uuid import uuid4 as _uuid4
    from app.core.config import settings as _settings

    correlation_id = x_correlation_id or str(_uuid4())
    logger = get_logger_with_correlation(correlation_id)

    # Validate Asaas authentication token (header: asaas-access-token)
    if _settings.ASAAS_WEBHOOK_TOKEN:
        incoming_token = request.headers.get("asaas-access-token", "")
        if not incoming_token or incoming_token != _settings.ASAAS_WEBHOOK_TOKEN:
            logger.warning(
                f"Asaas webhook rejected: invalid token. "
                f"Origin: {request.client.host if request.client else 'unknown'}"
            )
            # Return 200 to avoid Asaas retry storm, but take no action
            return {"received": False, "action": "rejected", "reason": "invalid_token"}

    event = payload.get("event", "")
    payment = payload.get("payment", {})
    payment_id = payment.get("id")

    logger.info(f"Asaas webhook received: event={event}, payment_id={payment_id}")

    # Handled events
    HANDLED_EVENTS = {
        "PAYMENT_RECEIVED",
        "PAYMENT_CONFIRMED",
        "TRANSFER_DONE",
        "TRANSFER_FAILED",
        "PAYMENT_REFUNDED",
        "PAYMENT_OVERDUE",
        "PAYMENT_DELETED",
        "PAYMENT_RESTORED",
    }
    if event not in HANDLED_EVENTS:
        return {"received": True, "action": "ignored", "event": event}

    # Transfer status events: update PixTransaction status
    if event in ("TRANSFER_DONE", "TRANSFER_FAILED"):
        transfer_id = payment.get("id") or payment.get("transferId")
        if transfer_id:
            pix_tx = db.query(PixTransaction).filter(PixTransaction.id == transfer_id).first()
            if pix_tx:
                if event == "TRANSFER_DONE":
                    pix_tx.status = PixStatus.CONFIRMED
                else:
                    # TRANSFER_FAILED: Asaas rejected/refunded the transfer.
                    # The balance was already deducted at dispatch time; restore it now.
                    pix_tx.status = PixStatus.FAILED
                    if pix_tx.type == TransactionType.SENT:
                        sender = db.query(User).filter(User.id == pix_tx.user_id).first()
                        if sender:
                            previous = sender.balance
                            sender.balance += pix_tx.value
                            db.add(sender)
                            logger.info(
                                f"TRANSFER_FAILED refund: user={sender.id}, "
                                f"amount=R${pix_tx.value:.2f}, "
                                f"balance: R${previous:.2f} -> R${sender.balance:.2f}"
                            )
                            audit_log(
                                action="transfer_failed_refund",
                                user=sender.id,
                                resource=f"pix_id={pix_tx.id}",
                                details={
                                    "amount": pix_tx.value,
                                    "previous_balance": previous,
                                    "new_balance": sender.balance,
                                    "transfer_id": transfer_id,
                                }
                            )
                db.add(pix_tx)
                db.commit()
                logger.info(f"Asaas webhook: transfer {transfer_id} updated to {event}")
        return {"received": True, "action": "transfer_updated", "event": event}

    # Refund / overdue / deleted / restored: log only, no balance mutation
    if event in ("PAYMENT_REFUNDED", "PAYMENT_OVERDUE", "PAYMENT_DELETED", "PAYMENT_RESTORED"):
        logger.info(f"Asaas webhook: lifecycle event {event} for payment {payment_id}")
        return {"received": True, "action": "logged", "event": event}

    # Only credit balance for confirmed/received events below
    if event not in ("PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"):
        return {"received": True, "action": "ignored", "event": event}

    if not payment_id:
        logger.warning("Asaas webhook: missing payment ID in payload")
        return {"received": True, "action": "ignored", "reason": "no_payment_id"}

    # Find pending charge in DB
    pix = db.query(PixTransaction).filter(
        PixTransaction.id == payment_id
    ).first()

    if not pix:
        logger.warning(f"Asaas webhook: charge not found in DB: {payment_id}")
        return {"received": True, "action": "ignored", "reason": "charge_not_found"}

    if pix.status.value == "CONFIRMADO":
        logger.info(f"Asaas webhook: charge already confirmed: {payment_id}")
        return {"received": True, "action": "already_confirmed"}

    # Confirm the transaction and credit balance
    pix.status = PixStatus.CONFIRMED
    db.add(pix)

    receiver_user = db.query(User).filter(User.id == pix.user_id).first()
    if receiver_user:
        previous_balance = receiver_user.balance
        receiver_user.balance += pix.value
        receiver_user.credit_limit += pix.value * 0.50
        db.add(receiver_user)
        logger.info(
            f"Asaas webhook confirmed deposit: user={receiver_user.id}, "
            f"amount=R${pix.value:.2f}, "
            f"balance: R${previous_balance:.2f} -> R${receiver_user.balance:.2f}"
        )

    db.commit()

    logger.info(f"Asaas webhook: charge {payment_id} confirmed automatically via webhook")
    return {"received": True, "action": "confirmed", "charge_id": payment_id}


@router.post("/webhook/asaas/validacao-saque", status_code=200)
async def asaas_withdrawal_validation(
    request: Request,
    x_correlation_id: str = Header(default=None)
):
    """
    Asaas withdrawal validation webhook.
    Receives a withdrawal request from Asaas and approves it instantly.
    Configure in Asaas: Mecanismos de seguranca > Validacao de saque > URL.
    URL: <APP_BASE_URL>/pix/webhook/asaas/validacao-saque
    Optional token: ASAAS_WITHDRAWAL_VALIDATION_TOKEN environment variable.
    """
    from uuid import uuid4 as _uuid4
    from app.core.config import settings as _settings

    correlation_id = x_correlation_id or str(_uuid4())
    logger = get_logger_with_correlation(correlation_id)

    # Validate optional authentication token if configured
    if _settings.ASAAS_WITHDRAWAL_VALIDATION_TOKEN:
        incoming_token = request.headers.get("asaas-access-token", "")
        if not incoming_token or incoming_token != _settings.ASAAS_WITHDRAWAL_VALIDATION_TOKEN:
            logger.warning(
                f"Withdrawal validation rejected: invalid token. "
                f"Origin: {request.client.host if request.client else 'unknown'}"
            )
            return {"status": "REFUSED", "refuseReason": "Unauthorized request"}

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # Asaas payload has "type" at root; transfer data nested by type
    withdrawal_type = payload.get("type", "")
    nested = (
        payload.get("transfer")
        or payload.get("bill")
        or payload.get("pixQrCode")
        or payload.get("mobilePhoneRecharge")
        or payload.get("pixRefund")
        or {}
    )
    withdrawal_id = nested.get("id", payload.get("id", "unknown"))
    withdrawal_value = nested.get("value", payload.get("value", 0))

    logger.info(
        f"Asaas withdrawal validation: type={withdrawal_type}, id={withdrawal_id}, "
        f"value=R${withdrawal_value} -> APPROVED"
    )

    # Approve all withdrawals — authorization is enforced at the application layer
    return {"status": "APPROVED"}


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
            receiver_doc = mask_cpf_cnpj(pix.pix_key)  # Best effort

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


# ============================================================================
# ASAAS INTEGRATION ENDPOINTS - Real PIX Operations
# ============================================================================

@router.post("/charges/create", response_model=Dict[str, Any], status_code=201)
def create_pix_charge_endpoint(
    value: float,
    description: str,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> Dict[str, Any]:
    """
    Creates a PIX charge (cobranca) with QR Code via Asaas.

    **Real Integration**: Generates actual PIX QR Code for payment collection.

    Args:
        value: Charge value in BRL (max R$ 1,000,000.00)
        description: Charge description (max 500 chars)

    Returns:
        {
            "charge_id": str,
            "qr_code": str,  # Copy-paste code
            "qr_code_url": str,  # Base64 QR Code image
            "value": float,
            "status": str,
            "created_at": datetime
        }
    """
    from app.pix.service import create_pix_charge_with_qrcode

    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        if value <= 0 or value > 1000000:
            raise HTTPException(status_code=400, detail="Value must be between 0.01 and 1,000,000.00")

        if not description or len(description) > 500:
            raise HTTPException(status_code=400, detail="Description is required and must be <= 500 chars")

        logger.info(f"Creating PIX charge for user {current_user.id}: value={value}, desc={description[:50]}")

        pix = create_pix_charge_with_qrcode(
            db=db,
            value=value,
            description=description,
            user_id=current_user.id,
            idempotency_key=x_idempotency_key,
            correlation_id=correlation_id
        )

        return {
            "charge_id": pix.id,
            "qr_code": pix.pix_key,  # QR Code copy-paste stored in pix_key
            "qr_code_url": None,  # TODO: Store QR Code image URL in database
            "value": pix.value,
            "status": pix.status.value,
            "created_at": pix.created_at
        }

    except ValueError as e:
        logger.warning(f"Validation error creating PIX charge: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating PIX charge: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error creating PIX charge")


@router.post("/payments/execute", response_model=Dict[str, Any])
def execute_pix_payment_endpoint(
    pix_transaction_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> Dict[str, Any]:
    """
    Executes a PIX payment (transferencia) via Asaas.

    **Real Integration**: Submits actual PIX transfer to Asaas gateway.

    Args:
        pix_transaction_id: Local PIX transaction ID (must be CREATED status)

    Returns:
        {
            "payment_id": str,
            "status": str,
            "end_to_end_id": str,
            "submitted_at": datetime
        }
    """
    from app.pix.service import execute_pix_payment_real

    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        # Fetch transaction
        pix = db.query(PixTransaction).filter(
            PixTransaction.id == pix_transaction_id,
            PixTransaction.user_id == current_user.id,
            PixTransaction.type == TransactionType.SENT
        ).first()

        if not pix:
            raise HTTPException(status_code=404, detail="PIX transaction not found or unauthorized")

        if pix.status != PixStatus.CREATED:
            raise HTTPException(
                status_code=400,
                detail=f"Transaction cannot be executed. Current status: {pix.status.value}"
            )

        logger.info(f"Executing PIX payment: id={pix_transaction_id}, value={pix.value}")

        success = execute_pix_payment_real(
            db=db,
            pix_transaction=pix,
            correlation_id=correlation_id
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to execute PIX payment via gateway")

        return {
            "payment_id": pix.id,
            "status": pix.status.value,
            "end_to_end_id": None,  # TODO: Store E2E ID from Asaas response
            "submitted_at": pix.updated_at
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing PIX payment: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error executing PIX payment")


@router.get("/charges/{charge_id}/sync", response_model=PixResponse)
def sync_pix_charge_status_endpoint(
    charge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> PixResponse:
    """
    Synchronizes PIX charge status with Asaas gateway.

    Fetches current status from Asaas and updates local database.

    Args:
        charge_id: PIX charge ID

    Returns:
        Updated transaction details
    """
    from app.pix.service import sync_pix_charge_status

    try:
        pix = sync_pix_charge_status(db, charge_id)

        if not pix:
            raise HTTPException(status_code=404, detail="PIX charge not found")

        if pix.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Unauthorized")

        return build_pix_response(pix, db)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing PIX charge status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error syncing status")
