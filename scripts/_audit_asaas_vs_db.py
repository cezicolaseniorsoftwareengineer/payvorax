import sys
sys.path.insert(0, '.')
import os
import httpx
from dotenv import load_dotenv
load_dotenv()

from app.core.database import SessionLocal
from app.auth.models import User
from app.cards.models import CreditCard

db = SessionLocal()
users = db.query(User).filter(User.balance > 0).all()
total_db = sum(float(u.balance) for u in users)

print("Saldos DB (usuarios com saldo > 0):")
for u in users:
    print(f"  {u.email}: R${float(u.balance):.2f}")

print(f"\nTotal DB:   R${total_db:.2f}")

key = os.getenv('ASAAS_API_KEY', '')
r = httpx.get(
    'https://api.asaas.com/v3/finance/balance',
    headers={'access_token': key},
    timeout=10
)
asaas_balance = r.json().get('balance', 0)
print(f"Asaas:      R${asaas_balance:.2f}")
print(f"Delta:      R${asaas_balance - total_db:.2f}")

db.close()
