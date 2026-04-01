"""
Reconcilia transações PROCESSING contra o status real no Asaas.

Para cada PixTransaction com status=PROCESSING e correlation_id preenchido:
  - Consulta GET /transfers/{correlation_id} na API Asaas
  - Se DONE     -> debita saldo do usuário, marca CONFIRMED
  - Se FAILED   -> marca FAILED (saldo já não foi debitado — deferred debit)
  - Se PENDING  -> deixa PROCESSING (aguarda webhook)

Uso:
  python scripts/reconcile_processing.py            # dry-run
  python scripts/reconcile_processing.py --apply    # aplica alterações
"""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app.cards.models, app.boleto.models, app.parcelamento.models
from app.core.database import SessionLocal
from app.pix.models import PixTransaction, TransactionType, PixStatus
from app.auth.models import User
from app.adapters.gateway_factory import get_payment_gateway
from decimal import Decimal

DRY_RUN = "--apply" not in sys.argv

db = SessionLocal()

processing_txs = db.query(PixTransaction).filter(
    PixTransaction.status == PixStatus.PROCESSING,
    PixTransaction.type == TransactionType.SENT,
).all()

if not processing_txs:
    print("Nenhuma transacao PROCESSING encontrada.")
    db.close()
    sys.exit(0)

print(f"Encontradas {len(processing_txs)} transacoes PROCESSING para reconciliar.\n")

gateway = get_payment_gateway()
if not gateway:
    print("ERRO: gateway nao configurado — variavel ASAAS_API_KEY ausente.")
    db.close()
    sys.exit(1)

for tx in processing_txs:
    correlation = tx.correlation_id
    if not correlation:
        print(f"  SKIP tx={tx.id} — sem correlation_id (nao foi para Asaas)")
        continue

    # Asaas transfer IDs comeam com 'tra_'; end_to_end_ids comecam com 'E'
    if not (correlation.startswith("tra_") or re.match(r"^[a-zA-Z0-9_-]{10,}", correlation)):
        print(f"  SKIP tx={tx.id} — correlation_id parece invalido: {correlation[:20]}")
        continue

    user = db.query(User).filter(User.id == tx.user_id).first()
    user_email = user.email if user else "desconhecido"

    print(f"\nConsultando Asaas: tx={tx.id} | correlation_id={correlation} | user={user_email}")

    try:
        asaas_status = gateway.get_payment_status(correlation)
    except Exception as e:
        print(f"  ERRO ao consultar Asaas: {e}")
        continue

    remote_status = asaas_status.get("status", "")
    print(f"  Asaas status: {remote_status}")

    if remote_status == "CONFIRMED":  # DONE mapeado para CONFIRMED no adapter
        fee = Decimal(str(tx.fee_amount or 0))
        total_debit = Decimal(str(tx.value)) + fee
        if user:
            prev_balance = Decimal(str(user.balance))
            new_balance = prev_balance - total_debit
            print(f"  ACAO: CONFIRMED -> debitar R${total_debit:.2f} | saldo {prev_balance:.2f} -> {new_balance:.2f}")
            if not DRY_RUN:
                user.balance = new_balance
                tx.status = PixStatus.CONFIRMED
                db.add(user)
                db.add(tx)
        else:
            print(f"  AVISO: usuario nao encontrado para tx={tx.id} — nao e possivel debitar")

    elif remote_status in ("FAILED", "CANCELLED"):
        print(f"  ACAO: FAILED -> marcar FAILED (saldo nao precisa ser debitado)")
        if not DRY_RUN:
            tx.status = PixStatus.FAILED
            db.add(tx)

    elif remote_status in ("PENDING", "PROCESSING"):
        print(f"  ACAO: NONE -> ainda em transito no Asaas, manter PROCESSING")

    else:
        print(f"  ACAO: NONE -> status desconhecido '{remote_status}', nao alterando")

if DRY_RUN:
    print("\n=== DRY RUN — nenhuma alteracao aplicada ===")
    print("Rode com --apply para corrigir:\n")
    print("  python scripts/reconcile_processing.py --apply\n")
    db.close()
    sys.exit(0)

try:
    db.commit()
    print("\nReconciliacao aplicada com sucesso.")
except Exception as e:
    db.rollback()
    print(f"\nERRO ao commitar: {e}")
    db.close()
    sys.exit(1)

# Verificar saldos finais
print("\nSaldos apos reconciliacao:")
users = db.query(User).filter(User.balance > 0).all()
for u in users:
    print(f"  {u.email} | balance={u.balance:.2f}")

db.close()
