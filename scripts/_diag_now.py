import sys
sys.path.insert(0, '.')
import os
import httpx
from dotenv import load_dotenv
load_dotenv()
from decimal import Decimal
from app.cards.models import CreditCard  # noqa: F401
from app.core.database import SessionLocal
from app.auth.models import User
from app.pix.models import PixTransaction, PixStatus, TransactionType
from app.core.config import settings

db = SessionLocal()

key = os.getenv('ASAAS_API_KEY', '')
r = httpx.get('https://api.asaas.com/v3/finance/balance', headers={'access_token': key}, timeout=10)
asaas_bal = r.json()['balance']

biocode = db.query(User).filter(User.email == 'biocodetechnology@gmail.com').first()
karen = db.query(User).filter(User.email == 'karenpassibrioli@gmail.com').first()
matrix = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()

print('Asaas balance: R$' + str(asaas_bal))
print('BioCode DB:    R$' + str(float(biocode.balance)))
print('Karen DB:      R$' + str(float(karen.balance)))
print('Matrix DB:     R$' + (str(float(matrix.balance)) if matrix else 'N/A'))
total_db = float(biocode.balance) + float(karen.balance) + (float(matrix.balance) if matrix else 0)
print('Total DB:      R$' + str(round(total_db, 2)))
print('Delta:         R$' + str(round(asaas_bal - total_db, 2)))

print()
print('Ultimas 6 txs BioCode:')
txs = (
    db.query(PixTransaction)
    .filter(PixTransaction.user_id == biocode.id)
    .order_by(PixTransaction.created_at.desc())
    .limit(6)
    .all()
)
for t in txs:
    print(
        '  '
        + t.type.value + ' | '
        + t.status.value + ' | '
        + 'val=' + str(t.value) + ' fee=' + str(t.fee_amount)
        + ' | ' + t.created_at.strftime('%d/%m %H:%M')
    )

db.close()
