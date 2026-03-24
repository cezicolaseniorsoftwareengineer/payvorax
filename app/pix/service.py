"""
Business logic for PIX transactions.
Implements idempotency, state machine transitions, and audit logging.
Integrates with Asaas BaaS for real PIX operations.
Uses internal balance transfer for BioCodeTechPay-to-BioCodeTechPay transactions.
"""
from uuid import uuid4
from typing import Optional, Dict, Any
from decimal import Decimal, ROUND_HALF_UP
import re
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.pix.models import (
    PixTransaction, PixStatus, TransactionType,
    LedgerEntry, LedgerEntryType, LedgerEntryStatus,
)
from app.pix.schemas import PixCreateRequest, PixKeyType
from app.core.logger import logger, audit_log
from app.core.security import mask_sensitive_data
from app.boleto.models import BoletoTransaction, BoletoStatus
from app.auth.models import User
from app.adapters.gateway_factory import get_payment_gateway
from app.pix.internal_transfer import find_recipient_user, execute_internal_transfer
from app.core.fees import calculate_pix_fee, fee_display, PLATFORM_PIX_OUTBOUND_NETWORK_FEE
from app.core.matrix import credit_fee
from datetime import datetime, timezone


def get_balance(db: Session, user_id: str) -> float:
    """
    Returns current account balance for a specific user.
    Raises ValueError if user not found or DB error occurs — callers must handle.
    Returning 0.0 on error would mask DB failures and show a false balance.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.error(f"User {user_id} not found")
        raise ValueError(f"User {user_id} not found")
    return float(user.balance)


def get_available_balance(db: Session, user_id: str) -> Decimal:
    """
    Available balance = current balance - sum of pending outbound (PROCESSING) transactions.
    Prevents overdraw when multiple transfers are in-flight awaiting webhook confirmation.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"User {user_id} not found")

    pending_outbound = db.query(
        func.coalesce(
            func.sum(
                PixTransaction.value + func.coalesce(PixTransaction.fee_amount, 0)
            ),
            0
        )
    ).filter(
        PixTransaction.user_id == user_id,
        PixTransaction.type == TransactionType.SENT,
        PixTransaction.status == PixStatus.PROCESSING,
    ).scalar()

    return Decimal(str(user.balance)) - Decimal(str(pending_outbound or 0))


def create_ledger_entry(
    db: Session,
    account_id: str,
    entry_type: LedgerEntryType,
    amount: Decimal,
    tx_id: str,
    description: str,
    status: LedgerEntryStatus = LedgerEntryStatus.PENDING,
) -> LedgerEntry:
    """Creates a ledger entry for auditable financial mutation tracking."""
    entry = LedgerEntry(
        id=str(uuid4()),
        account_id=account_id,
        entry_type=entry_type,
        amount=amount,
        status=status,
        tx_id=tx_id,
        description=description,
    )
    db.add(entry)
    return entry


def settle_ledger_entries(db: Session, tx_id: str) -> int:
    """Settles all PENDING ledger entries for a transaction. Returns count settled."""
    now = datetime.now(timezone.utc)
    entries = db.query(LedgerEntry).filter(
        LedgerEntry.tx_id == tx_id,
        LedgerEntry.status == LedgerEntryStatus.PENDING,
    ).all()
    for entry in entries:
        entry.status = LedgerEntryStatus.SETTLED
        entry.settled_at = now
        db.add(entry)
    return len(entries)


def reverse_ledger_entries(db: Session, tx_id: str) -> int:
    """Reverses all PENDING ledger entries for a transaction. Returns count reversed."""
    now = datetime.now(timezone.utc)
    entries = db.query(LedgerEntry).filter(
        LedgerEntry.tx_id == tx_id,
        LedgerEntry.status == LedgerEntryStatus.PENDING,
    ).all()
    for entry in entries:
        entry.status = LedgerEntryStatus.REVERSED
        entry.settled_at = now
        db.add(entry)
    return len(entries)


# Credit limit cap -- prevents unchecked growth of credit_limit from large deposits.
# A single R$1M deposit at 50% increment = R$500k limit without analysis.
# Cap at R$50,000 ensures any limit above this requires explicit review.
CREDIT_LIMIT_CAP = Decimal("50000.00")


def credit_pix_receipt(
    db: Session,
    receiver: User,
    gross_value: float,
    source: str,
    fee_override: Optional[float] = None,
):
    """
    Unified PIX receipt credit function.
    Eliminates code duplication across webhook confirm, lazy refresh,
    /receber/confirmar, /cobrar/{id}/verificar, and internal QR payments.

    Calculates receive fee (or uses fee_override), credits net amount to
    balance, increments credit_limit (capped at CREDIT_LIMIT_CAP), and
    credits fee to Matrix account.

    Returns:
        Tuple of (net_credit, receive_fee).
    """
    if fee_override is not None:
        receive_fee = round(fee_override, 2)
    else:
        receive_fee = round(float(calculate_pix_fee(
            receiver.cpf_cnpj,
            gross_value,
            is_external=True,
            is_received=True,
        )), 2)

    # Use Decimal arithmetic for precision in financial calculations
    _gross_dec = Decimal(str(gross_value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    _fee_dec = Decimal(str(receive_fee)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    _net_dec = max(Decimal("0.00"), _gross_dec - _fee_dec)

    net_credit = float(_net_dec)

    previous_balance = float(receiver.balance)
    _balance_dec = Decimal(str(receiver.balance)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    receiver.balance = (_balance_dec + _net_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    current_limit = Decimal(str(getattr(receiver, "credit_limit", 0) or 0))
    raw_increase = Decimal(str(gross_value)) * Decimal("0.50")
    capped_limit = min(current_limit + raw_increase, CREDIT_LIMIT_CAP)
    receiver.credit_limit = capped_limit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    db.add(receiver)

    if receive_fee > 0:
        credit_fee(db, receive_fee)

    audit_log(
        action="PIX_RECEIPT_CREDITED",
        user=str(receiver.id),
        resource=f"source={source}",
        details={
            "gross_value": gross_value,
            "receive_fee": receive_fee,
            "net_credit": net_credit,
            "previous_balance": previous_balance,
            "new_balance": receiver.balance,
            "credit_limit": receiver.credit_limit,
        }
    )

    logger.info(
        f"credit_pix_receipt: user={receiver.id}, source={source}, "
        f"gross=R${gross_value:.2f}, fee=R${receive_fee:.2f}, net=R${net_credit:.2f}, "
        f"balance: R${previous_balance:.2f} -> R${receiver.balance:.2f}, "
        f"credit_limit: {current_limit:.2f} -> R${receiver.credit_limit:.2f}"
    )

    return net_credit, receive_fee


def create_pix(
    db: Session,
    data: PixCreateRequest,
    idempotency_key: str,
    correlation_id: str,
    user_id: str,
    type: TransactionType = TransactionType.SENT
) -> PixTransaction:
    """
    Creates a PIX transaction with strict idempotency guarantees.
    Returns existing transaction if idempotency key collision occurs.
    """
    # Idempotency check
    existing_pix = db.query(PixTransaction).filter(
        PixTransaction.idempotency_key == idempotency_key
    ).first()

    if existing_pix:
        logger.info(f"Duplicate PIX detected (idempotency): key={idempotency_key}, id={existing_pix.id}")
        return existing_pix

    # Fee defaults: only set concretely on the external-sent path
    pix_fee = Decimal("0.00")

    # Balance Check for Outgoing Transactions (SENT)
    if type == TransactionType.SENT:
        if data.scheduled_date:
            initial_status = PixStatus.SCHEDULED
        else:
            # Immediate transaction - check if internal or external
            sender = db.query(User).filter(User.id == user_id).first()
            if not sender:
                raise ValueError(f"Sender user {user_id} not found")

            # Try to find recipient internally
            recipient = find_recipient_user(db, data.pix_key, data.key_type)

            if recipient:
                # Internal transfer using balance field
                logger.info(f"Internal transfer detected: {sender.name} -> {recipient.name}")

                sent_tx, recv_tx = execute_internal_transfer(
                    db=db,
                    sender=sender,
                    recipient=recipient,
                    amount=data.value,
                    pix_key=data.pix_key,
                    key_type=data.key_type.value,
                    description=data.description or "Internal Transfer",
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id
                )

                db.commit()
                db.refresh(sent_tx)

                return sent_tx
            else:
                # External transfer — validate available balance, dispatch via Asaas.
                # Balance debit is DEFERRED to webhook confirmation (TRANSFER_DONE).
                logger.info(f"External transfer detected for key: {data.pix_key} (type={data.key_type.value})")

                pix_fee = calculate_pix_fee(
                    sender.cpf_cnpj,
                    data.value,
                    is_external=True,
                    is_received=False,
                )
                total_required = Decimal(str(data.value)) + pix_fee

                # Available balance considers pending outbound (PROCESSING) transactions
                available = get_available_balance(db, user_id)
                if available < total_required:
                    raise ValueError(
                        f"Saldo insuficiente. Disponivel: R$ {available:.2f}, "
                        f"Necessario: R$ {total_required:.2f} "
                        f"(valor R$ {data.value:.2f} + taxa {fee_display(pix_fee)})"
                    )

                gateway = get_payment_gateway()

                # ── Persist-before-dispatch: create transaction BEFORE calling Asaas ──
                # This prevents orphaned PSP transactions if the app crashes between
                # dispatch and persist. Transaction starts as CREATED, moves to
                # PROCESSING after dispatch, or FAILED on error.
                initial_status = PixStatus.CREATED

    else:
        # Incoming transaction (Deposit/Charge)
        initial_status = PixStatus.CREATED

    # Create new transaction
    # pix_fee is Decimal("0.00") by default; overridden only on the external-sent path above

    pix = PixTransaction(
        id=str(uuid4()),
        value=data.value,
        pix_key=data.pix_key,
        key_type=data.key_type.value,
        type=type,
        status=initial_status,
        idempotency_key=idempotency_key,
        description=data.description,
        correlation_id=correlation_id,
        scheduled_date=data.scheduled_date,
        user_id=user_id,
        recipient_name=getattr(data, 'recipient_name', None),
        fee_amount=float(pix_fee),
    )

    db.add(pix)
    db.flush()  # persist row but keep transaction open for gateway dispatch

    # ── Gateway dispatch for external transfers (after persist) ──
    if type == TransactionType.SENT and initial_status == PixStatus.CREATED and pix_fee > Decimal("0.00"):
        if gateway:
            try:
                payment_result = gateway.create_pix_payment(
                    value=Decimal(str(data.value)),
                    pix_key=data.pix_key,
                    pix_key_type=data.key_type.value,
                    description=data.description or "BioCodeTechPay PIX Transfer",
                    idempotency_key=idempotency_key
                )
                end_to_end_id = payment_result.get("end_to_end_id") or payment_result.get("payment_id")
                logger.info(
                    f"Asaas PIX transfer dispatched: payment_id={payment_result.get('payment_id')}, "
                    f"end_to_end={end_to_end_id}, key={mask_sensitive_data(data.pix_key)}"
                )
                # Update transaction with Asaas response data
                if end_to_end_id:
                    pix.correlation_id = end_to_end_id
                pix.status = PixStatus.PROCESSING
            except Exception as e:
                pix.status = PixStatus.FAILED
                db.commit()
                logger.error(
                    f"Asaas PIX transfer failed: key={mask_sensitive_data(data.pix_key)}, "
                    f"error={str(e)}"
                )
                # Extract human-readable rejection reason from Asaas API response
                asaas_reason = ""
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        err_data = e.response.json()
                        descriptions = [
                            err.get("description", "")
                            for err in err_data.get("errors", [])
                            if err.get("description")
                        ]
                        if descriptions:
                            asaas_reason = " | ".join(descriptions)
                    except Exception:
                        pass
                raise ValueError(
                    f"Seu pix foi recusado: {asaas_reason}" if asaas_reason
                    else f"Falha ao processar transferencia PIX: {str(e)}"
                )
        else:
            # Gateway not configured (dev/local) — process locally without real dispatch
            pix.status = PixStatus.PROCESSING
            logger.warning(
                f"Payment gateway not configured. Transfer processed locally without real dispatch. "
                f"key={mask_sensitive_data(data.pix_key)}"
            )

    # Create PENDING ledger entry for external outbound transfers (deferred debit model).
    # Internal transfers and incoming transactions are settled immediately and don't need
    # pending ledger tracking.
    if pix.status == PixStatus.PROCESSING and type == TransactionType.SENT:
        create_ledger_entry(
            db=db,
            account_id=user_id,
            entry_type=LedgerEntryType.DEBIT,
            amount=Decimal(str(data.value)) + pix_fee,
            tx_id=pix.id,
            description=f"PIX outbound pending: {mask_sensitive_data(data.pix_key)}",
        )

    # Mask sensitive data in logs
    masked_key = mask_sensitive_data(data.pix_key)

    audit_log(
        action="pix_created",
        user=user_id,
        resource=f"pix_id={pix.id}",
        details={
            "correlation_id": correlation_id,
            "value": data.value,
            "masked_key": masked_key,
            "key_type": data.key_type.value,
            "transaction_type": type.value,
            "status": pix.status.value
        }
    )

    logger.info(f"PIX created (pending commit): id={pix.id}, value={data.value}, type={type.value}, status={pix.status.value}")

    try:
        db.commit()
        db.refresh(pix)
    except Exception as e:
        db.rollback()
        logger.error(f"Transaction failed, rolled back: {str(e)}")
        raise e

    return pix


def confirm_pix(
    db: Session,
    pix_id: str,
    correlation_id: str
) -> Optional[PixTransaction]:
    """
    Transitions transaction state to CONFIRMED.
    Simulates PSP callback processing.
    """
    pix = db.query(PixTransaction).filter(PixTransaction.id == pix_id).first()

    if not pix:
        logger.warning(f"PIX not found for confirmation: id={pix_id}")
        return None

    if pix.status == PixStatus.CONFIRMED:
        logger.info(f"PIX already confirmed: id={pix_id}")
        return pix

    # Update status
    pix.status = PixStatus.CONFIRMED
    db.commit()
    db.refresh(pix)

    audit_log(
        action="pix_confirmed",
        user="system",
        resource=f"pix_id={pix.id}",
        details={"correlation_id": correlation_id}
    )

    logger.info(f"PIX confirmed: id={pix.id}")

    return pix


def cancel_pix(
    db: Session,
    pix_id: str,
    user_id: str,
    correlation_id: str
) -> Optional[PixTransaction]:
    """
    Cancels a scheduled transaction.
    Only allows cancellation if status is SCHEDULED.
    """
    pix = db.query(PixTransaction).filter(
        PixTransaction.id == pix_id,
        PixTransaction.user_id == user_id
    ).first()

    if not pix:
        logger.warning(f"Attempt to cancel non-existent or unauthorized PIX: id={pix_id}")
        return None

    if pix.status != PixStatus.SCHEDULED:
        raise ValueError("Only scheduled transactions can be canceled.")

    # Update status
    pix.status = PixStatus.CANCELED
    db.commit()
    db.refresh(pix)

    audit_log(
        action="pix_canceled",
        user=user_id,
        resource=f"pix_id={pix.id}",
        details={"correlation_id": correlation_id}
    )

    logger.info(f"PIX canceled: id={pix.id}")

    return pix


def get_pix(db: Session, pix_id: str, user_id: str) -> Optional[PixTransaction]:
    """Retrieves transaction details by unique identifier and user."""
    return db.query(PixTransaction).filter(PixTransaction.id == pix_id, PixTransaction.user_id == user_id).first()


def list_statement(
    db: Session,
    user_id: str,
    limit: int = 50,
    status: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generates transaction ledger with aggregated totals for a specific user.
    """
    query = db.query(PixTransaction).filter(PixTransaction.user_id == user_id)

    if status:
        query = query.filter(PixTransaction.status == status)

    transactions = query.order_by(PixTransaction.created_at.desc()).limit(limit).all()

    # Use user.balance as the single authoritative source — never derived from transaction history
    user = db.query(User).filter(User.id == user_id).first()
    authoritative_balance = float(user.balance) if user else 0.0

    # Metrics for extrato summary cards (informational only, not used for balance)
    total_sent = db.query(func.sum(PixTransaction.value)).filter(
        PixTransaction.status == PixStatus.CONFIRMED,
        PixTransaction.type == TransactionType.SENT,
        PixTransaction.user_id == user_id
    ).scalar() or 0

    total_received = db.query(func.sum(PixTransaction.value)).filter(
        PixTransaction.status == PixStatus.CONFIRMED,
        PixTransaction.type == TransactionType.RECEIVED,
        PixTransaction.user_id == user_id
    ).scalar() or 0

    return {
        "total_transactions": len(transactions),
        "total_value": float(total_sent),
        "balance": authoritative_balance,
        "transactions": transactions
    }


# ============================================================================
# ASAAS INTEGRATION LAYER - Real PIX Operations
# ============================================================================

def ensure_asaas_customer(db: Session, user_id: str) -> Optional[str]:
    """
    Ensures user has an Asaas customer ID.
    Creates customer if not exists and stores in User model.

    Returns:
        Asaas customer ID or None if gateway not configured
    """
    gateway = get_payment_gateway()
    if not gateway:
        logger.warning("Payment gateway not configured. Skipping Asaas customer creation.")
        return None

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"User {user_id} not found")

    # Check if user already has Asaas customer ID
    # Note: This assumes User model has an 'asaas_customer_id' field
    # You may need to add this field to the User model via migration
    if hasattr(user, 'asaas_customer_id') and user.asaas_customer_id:
        return user.asaas_customer_id

    # Create customer on Asaas
    try:
        from app.adapters.asaas_adapter import AsaasAdapter
        customer_id = gateway.create_customer(
            name=user.name,
            cpf_cnpj=user.cpf_cnpj,
            email=user.email
        )

        # Store customer ID in User model
        if hasattr(user, 'asaas_customer_id'):
            user.asaas_customer_id = customer_id
            db.add(user)
            db.commit()
            logger.info(f"Asaas customer created: user_id={user_id}, customer_id={customer_id}")

        return customer_id
    except Exception as e:
        logger.error(f"Failed to create Asaas customer for user {user_id}: {str(e)}")
        raise


def create_pix_charge_with_qrcode(
    db: Session,
    value: float,
    description: str,
    user_id: str,
    idempotency_key: str,
    correlation_id: str
) -> PixTransaction:
    """
    Creates a PIX charge (receivable) with QR Code via Asaas.
    Stores transaction in database with CREATED status.

    Args:
        db: Database session
        value: Charge value in BRL
        description: Charge description
        user_id: User who will receive the payment
        idempotency_key: Idempotency key for duplicate prevention
        correlation_id: Correlation ID for tracing

    Returns:
        PixTransaction with QR Code data
    """
    gateway = get_payment_gateway()

    # Check if user has Asaas customer ID
    customer_id = ensure_asaas_customer(db, user_id)
    if not customer_id:
        # Fallback: create local transaction without gateway integration
        logger.warning("Asaas not configured. Creating local PIX charge without QR Code.")
        pix = PixTransaction(
            id=str(uuid4()),
            value=value,
            pix_key="local-charge-" + str(uuid4()),
            key_type=PixKeyType.RANDOM.value,
            type=TransactionType.RECEIVED,
            status=PixStatus.CREATED,
            idempotency_key=idempotency_key,
            description=description,
            correlation_id=correlation_id,
            user_id=user_id
        )
        db.add(pix)
        db.commit()
        db.refresh(pix)
        return pix

    # Create charge on Asaas with automatic split for platform fee collection.
    # When ASAAS_PLATFORM_WALLET_ID is configured, the R$3 inbound fee is routed
    # to BioCodeTechPay's own Asaas wallet at payment time — guaranteed by Asaas
    # infrastructure. The webhook handler still deducts the fee from the user's
    # DB balance for internal accounting.
    from app.core.config import settings as _settings
    from app.core.fees import PIX_INBOUND_NETWORK_FEE, PIX_MAINTENANCE_FEE
    _platform_wallet = _settings.ASAAS_PLATFORM_WALLET_ID
    # Fee is flat R$3.00 for all users (18/03/2026 policy): R$2 rede + R$1 manutencao
    _platform_fee: Optional[Decimal] = (PIX_INBOUND_NETWORK_FEE + PIX_MAINTENANCE_FEE) if _platform_wallet else None
    try:
        charge_data = gateway.create_pix_charge(
            value=Decimal(str(value)),
            description=description,
            customer_id=customer_id,
            idempotency_key=idempotency_key,
            platform_wallet_id=_platform_wallet,
            platform_fee=_platform_fee,
        )

        # Store transaction in database
        pix = PixTransaction(
            id=charge_data["charge_id"],  # Use Asaas payment ID
            value=value,
            pix_key=charge_data["qr_code"][:200],  # VARCHAR(200) — truncated but queryable via [:200] lookup
            key_type=PixKeyType.RANDOM.value,
            type=TransactionType.RECEIVED,
            status=PixStatus.CREATED,
            idempotency_key=idempotency_key,
            description=description,
            correlation_id=correlation_id,
            user_id=user_id
        )

        db.add(pix)
        db.commit()
        db.refresh(pix)

        audit_log(
            action="pix_charge_created",
            user=user_id,
            resource=f"pix_id={pix.id}",
            details={
                "correlation_id": correlation_id,
                "value": value,
                "asaas_charge_id": charge_data["charge_id"]
            }
        )

        logger.info(f"PIX charge created via Asaas: id={pix.id}, qr_code_length={len(charge_data['qr_code'])}")
        return pix

    except Exception as e:
        logger.error(f"Failed to create Asaas PIX charge: {str(e)}", exc_info=True)
        raise


def execute_pix_payment_real(
    db: Session,
    pix_transaction: PixTransaction,
    correlation_id: str
) -> bool:
    """
    Executes a PIX payment (transfer) via Asaas.
    Updates transaction status based on gateway response.

    Args:
        db: Database session
        pix_transaction: PIX transaction to execute
        correlation_id: Correlation ID for tracing

    Returns:
        True if payment was submitted successfully, False otherwise
    """
    gateway = get_payment_gateway()
    if not gateway:
        logger.warning("Payment gateway not configured. Skipping real PIX payment execution.")
        return False

    try:
        payment_data = gateway.create_pix_payment(
            value=Decimal(str(pix_transaction.value)),
            pix_key=pix_transaction.pix_key,
            pix_key_type=pix_transaction.key_type,
            description=pix_transaction.description or "PIX transfer",
            idempotency_key=pix_transaction.idempotency_key
        )

        # Update transaction with Asaas payment ID and status
        pix_transaction.status = PixStatus.PROCESSING
        pix_transaction.correlation_id = correlation_id

        db.add(pix_transaction)
        db.commit()

        audit_log(
            action="pix_payment_submitted",
            user=pix_transaction.user_id,
            resource=f"pix_id={pix_transaction.id}",
            details={
                "correlation_id": correlation_id,
                "value": pix_transaction.value,
                "asaas_payment_id": payment_data["payment_id"],
                "status": payment_data["status"]
            }
        )

        logger.info(f"PIX payment submitted via Asaas: id={pix_transaction.id}, status={payment_data['status']}")
        return True

    except Exception as e:
        logger.error(f"Failed to execute Asaas PIX payment: {str(e)}", exc_info=True)
        pix_transaction.status = PixStatus.FAILED
        db.add(pix_transaction)
        db.commit()
        return False


def sync_pix_charge_status(db: Session, charge_id: str) -> Optional[PixTransaction]:
    """
    Synchronizes PIX charge status with Asaas.
    Updates local transaction status based on gateway response.

    Args:
        db: Database session
        charge_id: PIX charge ID

    Returns:
        Updated PixTransaction or None if not found
    """
    gateway = get_payment_gateway()
    if not gateway:
        return None

    pix = db.query(PixTransaction).filter(
        PixTransaction.id == charge_id,
        PixTransaction.type == TransactionType.RECEIVED
    ).first()

    if not pix:
        logger.warning(f"PIX charge {charge_id} not found for status sync")
        return None

    try:
        status_data = gateway.get_charge_status(charge_id)

        # Map Asaas status to internal status
        status_map = {
            "PENDING": PixStatus.CREATED,
            "CONFIRMED": PixStatus.CONFIRMED,
            "EXPIRED": PixStatus.FAILED,
            "CANCELLED": PixStatus.CANCELED
        }

        new_status = status_map.get(status_data["status"], PixStatus.CREATED)

        if pix.status != new_status:
            pix.status = new_status
            db.add(pix)
            db.commit()
            logger.info(f"PIX charge status updated: id={charge_id}, old={pix.status}, new={new_status}")

        return pix

    except Exception as e:
        logger.error(f"Failed to sync PIX charge status: {str(e)}", exc_info=True)
        return None
