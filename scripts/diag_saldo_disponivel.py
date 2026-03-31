"""
Diagnóstico: verifica transações PROCESSING e saldo disponível.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app.cards.models, app.boleto.models, app.parcelamento.models
from app.core.database import SessionLocal
from app.pix.models import PixTransaction, TransactionType, PixStatus
from app.auth.models import User
from sqlalchemy import func
from decimal import Decimal

db = SessionLocal()

# --- Verificar transacoes PROCESSING ---
processing = db.query(PixTransaction).filter(
    PixTransaction.status == PixStatus.PROCESSING
).all()

if not processing:
    print("NENHUMA transacao PROCESSING encontrada no banco")
else:
    print(f"ENCONTRADAS {len(processing)} transacoes PROCESSING:")
    for t in processing:
        tp = t.type.value if hasattr(t.type, "value") else str(t.type)
        print(f"  [{tp}] user={t.user_id} val={t.value} fee={t.fee_amount} id={str(t.id)[:36]} created={t.created_at}")

print()

# --- Saldo disponivel para cada usuario ---
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
    flag = " <<< BLOQUEADO" if available < u.balance else ""
    print(f"  {u.email} | db_balance={u.balance:.2f} | processing_outbound={pending:.2f} | available={available:.2f}{flag}")

print()

# --- Pix keys registradas ---
from app.pix.models import PixKey
pix_keys = db.query(PixKey).all()
if pix_keys:
    print("PIX KEYS REGISTRADAS:")
    for k in pix_keys:
        print(f"  user={k.user_id} key={k.key_value} type={k.key_type} active={k.is_active}")
else:
    print("Nenhuma PIX key registrada na tabela pix_keys")

db.close()
