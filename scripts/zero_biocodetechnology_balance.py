"""
Zera o saldo da conta Bio Code Technology (CNPJ 61425124000103).

Motivo: saldo phantom — valor R$9.48 acumulado sem lastro em transacoes
PIX confirmadas (pagamentos externos processados pela Asaas sem deducao
interna correspondente, antes do fix de integridade financeira de
2026-03-16).

Invariantes:
  - Aritmetica Decimal com ROUND_HALF_UP.
  - Audit log emitido antes e apos.
  - Transacao atomica; rollback em qualquer falha.
  - Script idempotente: se saldo ja e zero, encerra sem modificar.
"""
import sys
import os
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.auth.models import User
from app.cards.models import CreditCard  # noqa: F401 — resolve ORM relationship
from app.core.logger import audit_log, logger

TARGET_CNPJ = "61425124000103"
TWO_PLACES  = Decimal("0.01")
REASON      = "balance_zeroing_phantom_balance_fix_2026_03_16"


def run() -> None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.cpf_cnpj == TARGET_CNPJ).first()
        if not user:
            print(f"[ERRO] Usuario nao encontrado: CNPJ {TARGET_CNPJ}")
            sys.exit(1)

        balance_current = Decimal(str(user.balance)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        if balance_current == Decimal("0.00"):
            print(f"[OK] Saldo ja zerado para {user.name} ({user.email}). Nenhuma alteracao.")
            return

        print(f"  Usuario      : {user.name}")
        print(f"  E-mail       : {user.email}")
        print(f"  CNPJ         : {user.cpf_cnpj}")
        print(f"  Saldo atual  : R$ {float(balance_current):.2f}")
        print(f"  Saldo final  : R$ 0.00")

        audit_log(
            action="MANUAL_BALANCE_ZEROING_PRE",
            user=user.email,
            resource="users.balance",
            details={
                "balance_before": float(balance_current),
                "balance_after": 0.0,
                "reason": REASON,
            },
        )

        user.balance = Decimal("0.00")
        db.add(user)
        db.commit()
        db.refresh(user)

        confirmed = Decimal(str(user.balance)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        audit_log(
            action="MANUAL_BALANCE_ZEROING_CONFIRMED",
            user=user.email,
            resource="users.balance",
            details={
                "balance_before": float(balance_current),
                "balance_after": float(confirmed),
                "reason": REASON,
            },
        )

        print(f"\n[OK] Saldo zerado com sucesso.")
        print(f"  Saldo confirmado no banco: R$ {float(confirmed):.2f}")

        if confirmed != Decimal("0.00"):
            print(
                f"[ALERTA] Confirmacao divergente: "
                f"esperado=0.00 real={float(confirmed):.2f}"
            )
            sys.exit(2)

    except Exception as exc:
        db.rollback()
        logger.error(f"zero_biocodetechnology_balance: rollback por excecao: {exc}")
        print(f"[ERRO] Rollback executado: {exc}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    run()
