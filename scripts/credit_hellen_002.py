"""
Manual credit script for fee precision recovery.

Context: corrects R$0.02 lost due to IEEE 754 float drift in PIX fee arithmetic
prior to the Decimal precision fix applied on 2026-03-12.

Usage:
    python scripts/credit_hellen_002.py <user_email>

The target email is passed as a CLI argument — never hardcoded — to prevent
PII from being embedded in versioned source code.
"""
import sys
import os
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.auth.models import User
from app.cards.models import CreditCard  # noqa: F401 — resolve ORM relationship
from app.core.logger import audit_log, logger

CREDIT_AMOUNT = Decimal("0.02")
TWO_PLACES = Decimal("0.01")
REASON = "fee_precision_recovery_decimal_fix_2026_03_12"


def run(target_email: str) -> None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == target_email).first()
        if not user:
            print(f"[ERRO] Usuario nao encontrado: {target_email}")
            sys.exit(1)

        balance_before = Decimal(str(user.balance)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        balance_after = (balance_before + CREDIT_AMOUNT).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        audit_log(
            action="MANUAL_CREDIT_PRE",
            user=user.id,
            resource="users.balance",
            details={
                "balance_before": float(balance_before),
                "credit": float(CREDIT_AMOUNT),
                "reason": REASON,
            },
        )

        user.balance = float(balance_after)
        db.commit()
        db.refresh(user)

        confirmed = Decimal(str(user.balance)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        audit_log(
            action="MANUAL_CREDIT_CONFIRMED",
            user=user.id,
            resource="users.balance",
            details={
                "balance_before": float(balance_before),
                "credit": float(CREDIT_AMOUNT),
                "balance_after": float(confirmed),
                "reason": REASON,
            },
        )

        print("=" * 56)
        print(f"  Usuario  : {user.name}")
        print(f"  Saldo antes : R$ {balance_before:.2f}")
        print(f"  Credito     : R$ {CREDIT_AMOUNT:.2f}")
        print(f"  Saldo apos  : R$ {confirmed:.2f}")
        print("  Status      : CONFIRMADO")
        print("=" * 56)

        if confirmed != balance_after:
            logger.error(
                f"[credit_002] Divergencia pos-commit: "
                f"esperado={float(balance_after):.2f} real={float(confirmed):.2f}"
            )
            sys.exit(2)

    except Exception as exc:
        db.rollback()
        logger.error(f"[credit_002] Rollback — {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python scripts/credit_hellen_002.py <user_email>")
        sys.exit(1)
    run(sys.argv[1])
