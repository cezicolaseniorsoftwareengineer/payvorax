"""
Fix: transações PROCESSING presas (orphaned) que bloqueiam get_available_balance().

Transações de 14-18/Mar/2026 cujos webhooks Asaas nunca chegaram.
Ação: marcar como FAILED para restaurar saldo disponível correto.

IDs afetados (Bio Code):
  e03c819e, bd548a19, b2dfcdd6, f1de62ef, 0e4f10dd, 297c1cd5
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app.cards.models, app.boleto.models, app.parcelamento.models
from app.core.database import SessionLocal
from app.pix.models import PixTransaction, TransactionType, PixStatus
from app.auth.models import User
from sqlalchemy import func
from decimal import Decimal
from datetime import datetime, timezone, timedelta

DRY_RUN = "--apply" not in sys.argv

db = SessionLocal()

# --- Encontrar todas transacoes PROCESSING com mais de 1 hora ---
cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

stuck = db.query(PixTransaction).filter(
    PixTransaction.status == PixStatus.PROCESSING,
    PixTransaction.type == TransactionType.SENT,
    PixTransaction.created_at < cutoff,
).all()

if not stuck:
    print("Nenhuma transacao PROCESSING presa encontrada.")
    db.close()
    sys.exit(0)

print(f"Encontradas {len(stuck)} transacoes PROCESSING presas (> 1h):\n")

total_liberated_by_user: dict = {}
for t in stuck:
    total = Decimal(str(t.value)) + Decimal(str(t.fee_amount or 0))
    uid = str(t.user_id)
    total_liberated_by_user[uid] = total_liberated_by_user.get(uid, Decimal("0")) + total

    u = db.query(User).filter(User.id == t.user_id).first()
    email = u.email if u else "desconhecido"
    print(f"  [{email}] val={t.value} fee={t.fee_amount} total_bloqueado={total:.2f} id={t.id} created={t.created_at}")

print()

if DRY_RUN:
    print("=== DRY RUN — nenhuma alteracao aplicada ===")
    print("Rode com --apply para corrigir:\n")
    print("  python scripts/fix_processing_stuck.py --apply\n")
    print("Saldo disponivel p/Bio Code apos correcao:")
    bio = db.query(User).filter(User.email == "biocodetechnology@gmail.com").first()
    if bio:
        bio_lib = total_liberated_by_user.get(str(bio.id), Decimal("0"))
        print(f"  R${Decimal(str(bio.balance)) + bio_lib:.2f} (atual: R${bio.balance:.2f})")
    db.close()
    sys.exit(0)

# --- APPLY ---
print("Marcando transacoes como FAILED...")
for t in stuck:
    t.status = PixStatus.FAILED
    db.add(t)

db.commit()

print(f"\n{len(stuck)} transacoes corrigidas para FAILED.\n")

# --- Verificar saldos apos correcao ---
print("Saldos disponíveis apos correcao:")
users = db.query(User).all()
for u in users:
    pending = db.query(
        func.coalesce(
            func.sum(PixTransaction.value + func.coalesce(PixTransaction.fee_amount, 0)),
            0
        )
    ).filter(
        PixTransaction.user_id == u.id,
        PixTransaction.type == TransactionType.SENT,
        PixTransaction.status == PixStatus.PROCESSING,
    ).scalar()
    available = Decimal(str(u.balance)) - Decimal(str(pending or 0))
    print(f"  {u.email} | db_balance={u.balance:.2f} | available={available:.2f}")

db.close()
print("\nOK: saldo disponivel restaurado.")
