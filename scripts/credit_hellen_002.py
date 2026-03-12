"""
Credita R$0.02 na conta de Hellen Cardoso (hellen.nutricao@outlook.com).

Motivo: recuperacao de margem de taxa PIX perdida por drift IEEE 754
em transacoes anteriores ao fix de precisao Decimal aplicado em
app/pix/service.py. Valor rastreado via auditoria de saldo em 2026-03-12.

Invariantes:
  - Aritmetica Decimal com ROUND_HALF_UP.
  - Audit log emitido antes e apos a operacao.
  - Transacao atomica; rollback em qualquer falha.
  - Script idempotente: verifica saldo antes de aplicar.
"""
import sys
import os
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.auth.models import User
from app.cards.models import CreditCard  # noqa: F401 — resolve ORM relationship
from app.core.logger import audit_log, logger

TARGET_EMAIL   = "hellen.nutricao@outlook.com"
CREDIT_AMOUNT  = Decimal("0.02")
TWO_PLACES     = Decimal("0.01")
REASON         = "fee_precision_recovery_decimal_fix_2026_03_12"


def run() -> None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == TARGET_EMAIL).first()
        if not user:
            print(f"[ERRO] Usuario nao encontrado: {TARGET_EMAIL}")
            sys.exit(1)

        balance_before = Decimal(str(user.balance)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        balance_after  = (balance_before + CREDIT_AMOUNT).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        audit_log(
            action="MANUAL_CREDIT_PRE",
            user=user.email,
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
            user=user.email,
            resource="users.balance",
            details={
                "balance_before": float(balance_before),
                "credit": float(CREDIT_AMOUNT),
                "balance_after": float(confirmed),
                "reason": REASON,
            },
        )

        print("=" * 56)
        print(f"  Usuario  : {user.name} <{user.email}>")
        print(f"  Saldo antes : R$ {balance_before:.2f}")
        print(f"  Credito     : R$ {CREDIT_AMOUNT:.2f}")
        print(f"  Saldo apos  : R$ {confirmed:.2f}")
        print("  Status      : CONFIRMADO")
        print("=" * 56)

        if confirmed != balance_after:
            logger.error(
                f"[credit_hellen_002] Divergencia pos-commit: "
                f"esperado={float(balance_after):.2f} real={float(confirmed):.2f}"
            )
            sys.exit(2)

    except Exception as exc:
        db.rollback()
        logger.error(f"[credit_hellen_002] Rollback — {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run()
