import sys
sys.path.insert(0, '.')
import app.cards.models, app.boleto.models, app.parcelamento.models
from app.core.database import SessionLocal
from app.auth.models import User
from app.pix.models import PixTransaction, TransactionType, PixStatus
from uuid import uuid4

db = SessionLocal()
u = db.query(User).filter(User.cpf_cnpj == '35060268870').first()
if not u:
    print('ERRO: usuario nao encontrado')
    db.close()
    sys.exit(1)

bal = u.balance
print('Usuario: ' + str(u.name) + ' | Saldo atual: R$' + str(bal))

if abs(bal - 15.0) > 0.01:
    print('AVISO: saldo esperado R$15,00, encontrado R$' + str(bal))
    print('Abortando - verificar manualmente')
    db.close()
    sys.exit(1)

u.balance = 5.0
tx = PixTransaction(
    id=str(uuid4()),
    value=10.0,
    pix_key='reconciliacao-manual',
    key_type='EVP',
    type=TransactionType.SENT,
    status=PixStatus.CONFIRMED,
    user_id=u.id,
    idempotency_key='reconcil-cezi-excess-20260317',
    description='Estorno credito duplicado pay_p5mj8neo4d268p47',
    fee_amount=0.0
)
db.add(tx)
db.add(u)
db.commit()
db.close()
print('OK: saldo Cezi Cola -> R$5.00 | estorno registrado')
