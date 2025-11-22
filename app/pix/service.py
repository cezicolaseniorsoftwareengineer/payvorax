"""
Business logic for PIX transactions.
Implements idempotency, state machine transitions, and audit logging.
"""
from uuid import uuid4
from typing import Optional, Dict, Any
import re
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.pix.schemas import PixCreateRequest, PixKeyType
from app.core.logger import logger, audit_log
from app.core.security import mask_sensitive_data
from app.boleto.models import BoletoTransaction, BoletoStatus
from app.auth.models import User


def get_balance(db: Session, user_id: str) -> float:
    """Calculates current account balance for a specific user."""
    try:
        total_sent = db.query(func.sum(PixTransaction.value)).filter(
            PixTransaction.status == PixStatus.CONFIRMED,
            PixTransaction.type == TransactionType.SENT,
            PixTransaction.user_id == user_id
        ).scalar() or 0.0

        total_received = db.query(func.sum(PixTransaction.value)).filter(
            PixTransaction.status == PixStatus.CONFIRMED,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.user_id == user_id
        ).scalar() or 0.0

        total_boleto_paid = db.query(func.sum(BoletoTransaction.value)).filter(
            BoletoTransaction.status == BoletoStatus.PAID,
            BoletoTransaction.user_id == user_id
        ).scalar() or 0.0

        return float(total_received - total_sent - total_boleto_paid)
    except Exception as e:
        logger.error(f"Error calculating balance for user {user_id}: {str(e)}")
        return 0.0


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

    # Check for "Copia e Cola" Self-Deposit (Deposit Simulation)
    # This allows a user to "pay" their own charge to simulate a deposit without needing initial balance.
    if data.key_type == PixKeyType.RANDOM and len(data.pix_key) > 36:
        match = re.search(r'0136([0-9a-fA-F-]{36})', data.pix_key)
        if match:
            charge_id = match.group(1)
            charge_transaction = db.query(PixTransaction).filter(
                PixTransaction.id == charge_id,
                PixTransaction.type == TransactionType.RECEIVED,
                PixTransaction.status == PixStatus.CREATED
            ).first()

            if charge_transaction and charge_transaction.user_id == user_id:
                logger.info(f"Self-Deposit detected for user {user_id}. Confirming charge {charge_id} without debit.")

                # Confirm the charge
                charge_transaction.status = PixStatus.CONFIRMED
                charge_transaction.correlation_id = correlation_id

                # Apply Credit Limit Increase
                recipient_user = db.query(User).filter(User.id == user_id).first()
                if recipient_user:
                    limit_increase = charge_transaction.value * 0.50
                    recipient_user.credit_limit += limit_increase
                    db.add(recipient_user)

                db.add(charge_transaction)
                db.commit()
                db.refresh(charge_transaction)

                audit_log(
                    action="pix_deposit_simulated",
                    user=user_id,
                    resource=f"pix_id={charge_transaction.id}",
                    details={
                        "value": charge_transaction.value,
                        "correlation_id": correlation_id
                    }
                )
                return charge_transaction

    # Balance Check for Outgoing Transactions
    if type == TransactionType.SENT:
        if data.scheduled_date:
            # Scheduled transaction
            initial_status = PixStatus.SCHEDULED
        else:
            # Immediate transaction - Check Balance
            current_balance = get_balance(db, user_id)
            if data.value > current_balance:
                raise ValueError("Insufficient balance")
            initial_status = PixStatus.CONFIRMED
    else:
        # Incoming transaction (Deposit)
        initial_status = PixStatus.CREATED

    # Create new transaction
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
        user_id=user_id
    )

    db.add(pix)
    # REMOVED INTERMEDIATE COMMIT to ensure atomicity
    # db.commit()
    # db.refresh(pix)

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
            "status": initial_status.value
        }
    )

    logger.info(f"PIX created (pending commit): id={pix.id}, value={data.value}, type={type.value}, status={initial_status.value}")

    # Real-time Internal Transfer Logic
    # If the destination key belongs to a local user, credit them immediately.
    if type == TransactionType.SENT and initial_status != PixStatus.SCHEDULED:
        recipient_user = None
        charge_transaction = None

        # 1. Check if it's a "Copia e Cola" payment (Charge ID extraction)
        if data.key_type == PixKeyType.RANDOM and len(data.pix_key) > 36:
            # Regex to find UUID in the standard EMV payload (0136...)
            match = re.search(r'0136([0-9a-fA-F-]{36})', data.pix_key)
            if match:
                charge_id = match.group(1)
                logger.info(f"Detected Copia e Cola payment. Charge ID: {charge_id}")

                # Find the pending charge
                charge_transaction = db.query(PixTransaction).filter(
                    PixTransaction.id == charge_id,
                    PixTransaction.type == TransactionType.RECEIVED,
                    PixTransaction.status == PixStatus.CREATED
                ).first()

                if charge_transaction:
                    recipient_user = db.query(User).filter(User.id == charge_transaction.user_id).first()
                    if recipient_user:
                        logger.info(f"Paying Charge for User: {recipient_user.name}")

                        # Confirm the existing charge transaction
                        charge_transaction.status = PixStatus.CONFIRMED
                        charge_transaction.correlation_id = correlation_id  # Link transactions
                        # Optional: Update value if partial payment allowed (not implemented here)

                        db.add(charge_transaction)

                        # Apply Credit Limit Increase
                        limit_increase = data.value * 0.50
                        recipient_user.credit_limit += limit_increase
                        db.add(recipient_user)

                        logger.info(f"Charge {charge_id} confirmed. Credit limit increased.")
                    else:
                        logger.warning(f"Charge owner not found: {charge_transaction.user_id}")
                else:
                    logger.warning(f"Charge not found or already paid: {charge_id}")

        # 2. If not a charge, search for recipient by Key (CPF/Email)
        if not recipient_user:
            if data.key_type in [PixKeyType.CPF, PixKeyType.CNPJ]:
                # Normalize key: remove non-digits
                clean_key = re.sub(r'\D', '', data.pix_key)
                logger.info(f"Searching for recipient with CPF/CNPJ: {clean_key}")
                recipient_user = db.query(User).filter(User.cpf_cnpj == clean_key).first()
            elif data.key_type == PixKeyType.EMAIL:
                email_key = data.pix_key.strip().lower()
                logger.info(f"Searching for recipient with Email: {email_key}")
                recipient_user = db.query(User).filter(func.lower(User.email) == email_key).first()

            # If recipient found via Key, create the RECEIVED transaction
            if recipient_user:
                logger.info(f"Recipient found: {recipient_user.name} (ID: {recipient_user.id})")

                # Create incoming transaction for recipient
                received_pix = PixTransaction(
                    id=str(uuid4()),
                    value=data.value,
                    pix_key=data.pix_key,
                    key_type=data.key_type.value,
                    type=TransactionType.RECEIVED,
                    status=PixStatus.CONFIRMED,
                    idempotency_key=f"internal-{idempotency_key}",
                    description=data.description or "Transferência Recebida",
                    correlation_id=correlation_id,
                    user_id=recipient_user.id
                )
                db.add(received_pix)

                # Apply Credit Limit Increase Rule (50% of received amount)
                limit_increase = data.value * 0.50
                recipient_user.credit_limit += limit_increase
                db.add(recipient_user)

                logger.info(f"Internal transfer executed: {data.value} to {recipient_user.name} (ID: {recipient_user.id})")
                logger.info(f"Credit limit for {recipient_user.name} increased by R$ {limit_increase:.2f}")
            else:
                # Only log warning if it wasn't a charge attempt (which logs its own warning)
                if not (data.key_type == PixKeyType.RANDOM and len(data.pix_key) > 36):
                    logger.warning(f"Recipient NOT found for key: {data.pix_key} (Type: {data.key_type})")

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

    # Calculate totals
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

    balance = total_received - total_sent

    return {
        "total_transactions": len(transactions),
        "total_value": float(total_sent),  # Keeping for backward compatibility if needed
        "balance": float(balance),
        "transactions": transactions
    }
