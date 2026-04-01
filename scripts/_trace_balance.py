import sys
sys.path.insert(0, '.')
from app.core.database import SessionLocal
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.cards.models import CreditCard

db = SessionLocal()
u = db.query(User).filter(User.email == 'biocodetechnology@gmail.com').first()
print(f"db_balance atual: {u.balance}")

txs = (
    db.query(PixTransaction)
    .filter(PixTransaction.user_id == u.id)
    .order_by(PixTransaction.created_at.asc())
    .all()
)

running = 0.0
print("\nEvolucao do saldo:")
print(f"{'Data':14} | {'Tipo':10} | {'Status':12} | {'Valor':>9} | {'Taxa':>7} | {'Saldo':>10}")
print("-" * 80)
for t in txs:
    val = float(t.value) if t.value else 0.0
    fee = float(t.fee_amount) if t.fee_amount else 0.0
    note = ""
    if t.type == TransactionType.RECEIVED and t.status == PixStatus.CONFIRMED:
        running += val
    elif t.type == TransactionType.SENT and t.status == PixStatus.CONFIRMED:
        running -= (val + fee)
    elif t.status == PixStatus.PROCESSING:
        note = " << PROCESSANDO"
    dt = t.created_at.strftime("%d/%m %H:%M")
    print(f"{dt:14} | {t.type.value:10} | {t.status.value:12} | {val:9.2f} | {fee:7.2f} | {running:10.2f}{note}")

print(f"\nSaldo DB salvo:       {float(u.balance):10.2f}")
print(f"Saldo calculado:      {running:10.2f}")
print(f"Diferenca:            {float(u.balance) - running:10.2f}")
db.close()
