"""
Internal PIX transfer logic for BioCodeTechPay users.
Handles peer-to-peer transfers without external gateway integration.
Internal transfers are free (R$0 fee). No Asaas API call.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func
import re
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.pix.schemas import PixKeyType
from app.core.logger import logger, audit_log
from app.core.security import mask_sensitive_data

from uuid import uuid4

_TWO_PLACES = Decimal("0.01")


def find_recipient_user(
    db: Session,
    pix_key: str,
    key_type: PixKeyType
) -> Optional[User]:
    """
    Finds BioCodeTechPay user by PIX key.

    Args:
        db: Database session
        pix_key: PIX key (CPF, email, phone, random)
        key_type: Type of PIX key

    Returns:
        User if found in BioCodeTechPay, None otherwise
    """
    try:
        if key_type in [PixKeyType.CPF, PixKeyType.CNPJ]:
            clean_key = re.sub(r'\D', '', pix_key)
            logger.info(f"Searching internal user by CPF/CNPJ: {mask_sensitive_data(clean_key)}")
            return db.query(User).filter(User.cpf_cnpj == clean_key).first()

        elif key_type == PixKeyType.EMAIL:
            email_key = pix_key.strip().lower()
            logger.info(f"Searching internal user by EMAIL: {mask_sensitive_data(email_key, visible_chars=3)}")
            # Check login email first, then pix_email_key (may differ)
            user = db.query(User).filter(func.lower(User.email) == email_key).first()
            if not user:
                user = db.query(User).filter(
                    func.lower(User.pix_email_key) == email_key
                ).first()
            return user

        elif key_type == PixKeyType.RANDOM:
            random_key = pix_key.strip().lower()
            logger.info(f"Searching internal user by random key: {random_key[:8]}...")
            return db.query(User).filter(
                func.lower(User.pix_random_key) == random_key
            ).first()

        logger.info(f"Key type {key_type} not supported for internal search")
        return None

    except Exception as e:
        logger.error(f"Error searching recipient user: {str(e)}")
        return None


def execute_internal_transfer(
    db: Session,
    sender: User,
    recipient: User,
    amount: float,
    pix_key: str,
    key_type: str,
    description: str,
    idempotency_key: str,
    correlation_id: str
) -> Tuple[PixTransaction, PixTransaction]:
    """
    Executes internal transfer between BioCodeTechPay users.
    Updates balance fields directly without external gateway.

    Args:
        db: Database session
        sender: Sender user
        recipient: Recipient user
        amount: Transfer amount
        pix_key: PIX key used
        key_type: Type of key
        description: Transfer description
        idempotency_key: Idempotency key for sender transaction
        correlation_id: Correlation ID for tracking

    Returns:
        Tuple of (sent_transaction, received_transaction)

    Raises:
        ValueError: If sender has insufficient balance
    """
    # Use Decimal arithmetic to avoid IEEE 754 float drift (e.g. 3.1499999... < 3.15).
    # Internal transfers are free: no taxa de rede, no taxa de manutencao.
    _sender_dec = Decimal(str(sender.balance)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    _amount_dec = Decimal(str(amount)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    _fee_dec    = Decimal("0.00")  # Internal transfers are free

    # Self-transfer guard: sender and recipient are the same account.
    # This produces a financial no-op (debit and credit cancel out) while creating
    # confusing transaction records. Block it early with a clear message.
    if sender.id == recipient.id:
        raise ValueError(
            "Nao e possivel transferir para a propria conta. "
            "Para depositar fundos externos, use 'Cobrar' para gerar seu QR Code."
        )

    if _sender_dec < _amount_dec:
        raise ValueError(
            f"Saldo insuficiente. Disponivel: R$ {float(_sender_dec):.2f}. "
            f"Necessario: R$ {float(_amount_dec):.2f}."
        )

    sent_tx = PixTransaction(
        id=str(uuid4()),
        value=amount,
        pix_key=pix_key,
        key_type=key_type,
        type=TransactionType.SENT,
        status=PixStatus.CONFIRMED,
        idempotency_key=idempotency_key,
        description=description or "Internal Transfer Sent",
        correlation_id=correlation_id,
        user_id=sender.id
    )

    received_tx = PixTransaction(
        id=str(uuid4()),
        value=amount,
        pix_key=pix_key,
        key_type=key_type,
        type=TransactionType.RECEIVED,
        status=PixStatus.CONFIRMED,
        idempotency_key=f"internal-recv-{idempotency_key}",
        description=description or "Internal Transfer Received",
        correlation_id=correlation_id,
        user_id=recipient.id
    )

    # Apply balance changes using Decimal to preserve precision.
    _sender_new = (_sender_dec - _amount_dec - _fee_dec).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    _recip_dec  = Decimal(str(recipient.balance)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    _recip_new  = (_recip_dec + _amount_dec).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    sender.balance    = _sender_new
    recipient.balance = _recip_new

    db.add(sent_tx)
    db.add(received_tx)
    db.add(sender)
    db.add(recipient)
    audit_log(
        action="internal_pix_transfer",
        user=sender.id,
        resource=f"sent_tx={sent_tx.id}, recv_tx={received_tx.id}",
        details={
            "sender_id": sender.id,
            "recipient_id": recipient.id,
            "amount": amount,
            "sender_new_balance": sender.balance,
            "recipient_new_balance": recipient.balance,
            "correlation_id": correlation_id
        }
    )

    logger.info(
        f"Internal transfer executed: {amount:.2f} from {sender.name} "
        f"(balance: {sender.balance + _amount_dec:.2f} -> {sender.balance:.2f}) "
        f"to {recipient.name} (balance: {recipient.balance - _amount_dec:.2f} -> {recipient.balance:.2f})"
    )

    return sent_tx, received_tx
