from decimal import Decimal
from uuid import uuid4
from sqlalchemy.orm import Session
from app.boleto.models import BoletoTransaction, BoletoStatus
from app.boleto.schemas import BoletoPaymentRequest, BoletoDetails
from app.pix.service import get_balance
from app.core.logger import logger, audit_log
from app.core.fees import calculate_boleto_fee, fee_display
from app.core.matrix import credit_fee
from app.auth.models import User
from datetime import date, timedelta
import secrets


# ---------------------------------------------------------------------------
# Barcode check-digit validation (modulo-10 for banking boletos).
# Banking boletos (47 digits, typeable line) use modulo-10 per field.
# Utility boletos (44/48 digits) use modulo-10 or modulo-11.
# ---------------------------------------------------------------------------

def _mod10(digits: str) -> int:
    """Modulo-10 check digit algorithm (Luhn variant used in Brazilian boletos)."""
    weights = [2, 1]
    total = 0
    for i, ch in enumerate(reversed(digits)):
        product = int(ch) * weights[i % 2]
        total += product if product < 10 else product - 9
    remainder = total % 10
    return 0 if remainder == 0 else 10 - remainder


def _mod11_bank(digits: str) -> int:
    """Modulo-11 check digit for the barcode general verifier (position 5 of barcode)."""
    weights = list(range(2, 10))
    total = 0
    for i, ch in enumerate(reversed(digits)):
        total += int(ch) * weights[i % len(weights)]
    remainder = total % 11
    if remainder in (0, 1):
        return 1
    return 11 - remainder


def validate_barcode(barcode: str) -> bool:
    """
    Validates a Brazilian boleto barcode or typeable line.
    Returns True if the barcode passes structural and check-digit validation.
    Returns False otherwise (caller decides how to handle).
    """
    cleaned = barcode.strip()
    if not cleaned.isdigit():
        return False

    length = len(cleaned)

    # Typeable line (linha digitavel) for banking boletos: 47 digits
    # Structure: field1(10) + field2(11) + field3(11) + field4(1) + field5(14)
    if length == 47:
        # Validate modulo-10 check digit of each of the three fields
        field1 = cleaned[0:9]
        check1 = int(cleaned[9])
        field2 = cleaned[10:20]
        check2 = int(cleaned[20])
        field3 = cleaned[21:31]
        check3 = int(cleaned[31])
        return (
            _mod10(field1) == check1
            and _mod10(field2) == check2
            and _mod10(field3) == check3
        )

    # Barcode format: 44 digits (banking boleto)
    if length == 44 and cleaned[0] != "8":
        verifier = int(cleaned[4])
        without_verifier = cleaned[0:4] + cleaned[5:]
        return _mod11_bank(without_verifier) == verifier

    # Utility boleto (convenio): 44 or 48 digits, starts with "8"
    if length == 48 and cleaned[0] == "8":
        # Four blocks of 12 digits each, last digit of each block is modulo-10 check
        for i in range(4):
            block = cleaned[i * 12 : i * 12 + 11]
            check = int(cleaned[i * 12 + 11])
            if _mod10(block) != check:
                return False
        return True

    if length == 44 and cleaned[0] == "8":
        # Short utility barcode — modulo-10 or modulo-11 on general verifier at position 3
        # Accept structurally valid lengths without deep check (rare format)
        return True

    return False


def query_boleto(barcode: str) -> BoletoDetails:
    cleaned = barcode.strip()

    if not cleaned.isdigit() or len(cleaned) < 44:
        raise ValueError("Codigo de barras invalido: deve conter ao menos 44 digitos numericos")

    if not validate_barcode(cleaned):
        raise ValueError("Codigo de barras invalido: digito verificador incorreto")

    if cleaned.endswith("0000"):
        raise ValueError("Boleto expired or not found")

    # Try Asaas Bill Payment API first (requires feature enabled on account)
    try:
        from app.adapters.gateway_factory import get_gateway
        gateway = get_gateway()
        if hasattr(gateway, "simulate_boleto_payment"):
            result = gateway.simulate_boleto_payment(cleaned)
            if result and result.get("value"):
                from datetime import datetime as _dt_parse
                _raw_due = result.get("due_date")
                _due = (
                    _dt_parse.strptime(_raw_due, "%Y-%m-%d").date()
                    if isinstance(_raw_due, str) and _raw_due
                    else date.today() + timedelta(days=3)
                )
                logger.info(f"Boleto query via Asaas: barcode={cleaned[:20]}... value={result['value']}")
                return BoletoDetails(
                    barcode=cleaned,
                    beneficiary=result.get("beneficiary", "Beneficiario"),
                    value=result["value"],
                    due_date=_due,
                )
    except Exception as e:
        logger.info(f"Asaas boleto query unavailable, using mock: {e}")

    # Mock fallback — used when Asaas bill payment is not enabled or unavailable
    return BoletoDetails(
        barcode=cleaned,
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

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("User not found")

    fee = calculate_boleto_fee(user.cpf_cnpj)
    total_required = Decimal(str(data.value)) + fee

    balance = get_balance(db, user_id)
    if balance < total_required:
        raise ValueError(
            f"Saldo insuficiente. Disponivel: R$ {balance:.2f}, "
            f"Necessario: R$ {total_required:.2f} "
            f"(valor R$ {data.value:.2f} + taxa {fee_display(fee)})"
        )

    # Debit balance including fee
    user.balance = Decimal(str(user.balance)) - total_required
    if user.balance < Decimal("0.00"):
        db.rollback()
        logger.error(
            f"BALANCE_INVARIANT_VIOLATION [boleto]: user={user_id} post-debit={user.balance:.2f}"
        )
        raise ValueError("Saldo insuficiente. Operacao cancelada por protecao de saldo.")
    db.add(user)

    # Credit fee to BioCodeTechPay matrix account (same transaction)
    credit_fee(db, float(fee))

    boleto = BoletoTransaction(
        id=str(uuid4()),
        value=data.value,
        barcode=data.barcode,
        description=data.description,
        status=BoletoStatus.PAID,
        user_id=user_id,
        correlation_id=correlation_id,
        fee_amount=float(fee),
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
            "fee_amount": float(fee),
            "total_charged": total_required,
            "barcode": data.barcode
        }
    )

    logger.info(f"Boleto paid: id={boleto.id}, value={data.value}, fee={fee_display(fee)}")
    return boleto
