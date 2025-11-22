from uuid import uuid4
from sqlalchemy.orm import Session
from app.boleto.models import BoletoTransaction, BoletoStatus
from app.boleto.schemas import BoletoPaymentRequest, BoletoDetails
from app.pix.service import get_balance
from app.core.logger import logger, audit_log
from datetime import date, timedelta
import secrets


def query_boleto(barcode: str) -> BoletoDetails:
    # Mock validation
    if not barcode.isdigit() or len(barcode) < 44:
        raise ValueError("Invalid barcode")

    if barcode.endswith("0000"):
        raise ValueError("Boleto expired or not found")

    # Mock details
    return BoletoDetails(
        barcode=barcode,
        beneficiary=f"Mock Company {secrets.randbelow(100) + 1} LTDA",
        value=float(f"{secrets.randbelow(491) + 10}.{secrets.randbelow(100)}"),
        due_date=date.today() + timedelta(days=secrets.randbelow(10) + 1)
    )


def process_payment(
    db: Session,
    data: BoletoPaymentRequest,
    user_id: str,
    correlation_id: str
) -> BoletoTransaction:

    balance = get_balance(db, user_id)
    if balance < data.value:
        raise ValueError("Insufficient balance")

    boleto = BoletoTransaction(
        id=str(uuid4()),
        value=data.value,
        barcode=data.barcode,
        description=data.description,
        status=BoletoStatus.PAID,
        user_id=user_id,
        correlation_id=correlation_id
    )

    db.add(boleto)
    db.commit()
    db.refresh(boleto)

    audit_log(
        action="boleto_paid",
        user=user_id,
        resource=f"boleto_id={boleto.id}",
        details={
            "correlation_id": correlation_id,
            "value": data.value,
            "barcode": data.barcode
        }
    )

    logger.info(f"Boleto paid: id={boleto.id}, value={data.value}")
    return boleto
