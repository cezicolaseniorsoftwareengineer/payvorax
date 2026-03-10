import sys
import os

# FORCE LOCAL SQLITE
os.environ["DATABASE_URL"] = "sqlite:///./fintech.db"
sys.path.append(os.getcwd())

from app.core.database import SessionLocal
from app.auth.models import User
from app.auth.service import get_password_hash
import app.cards.models  # Required for relationship mapping

def fix_identities():
    db = SessionLocal()

    print("--- 1. CORRIGINDO IDENTIDADE BIO CODE TECHNOLOGY ---")
    cnpj_biocode = "61425124000103"
    user_biocode = db.query(User).filter(User.cpf_cnpj == cnpj_biocode).first()

    if user_biocode:
        user_biocode.name = "Bio Code Technology"
        user_biocode.email = "financeiro@biocodetech.com"  # Email placeholder adequado
        db.commit()
        print(f"[OK] Conta {cnpj_biocode} atualizada para nome: {user_biocode.name}")
    else:
        print(f"[ERRO] Conta Bio Code {cnpj_biocode} não encontrada para corrigir.")

    print("\n--- 2. CRIANDO CONTA Admin Bio Code Tech Pay (SEPARADA) ---")
    cpf_admin = "00000000000"  # CPF virtual para Admin do Sistema
    user_admin = db.query(User).filter(User.cpf_cnpj == cpf_admin).first()

    admin_pass = "admin.Bio Code Tech Pay"
    hashed_admin = get_password_hash(admin_pass)

    if not user_admin:
        user_admin = User(
            name="Admin Bio Code Tech Pay",
            email="root@biocodetechpay.com",
            cpf_cnpj=cpf_admin,
            hashed_password=hashed_admin,
            credit_limit=1000000.00
        )
        db.add(user_admin)
        print(f"[CRIADO] Novo Admin do Sistema criado.")
    else:
        user_admin.name = "Admin Bio Code Tech Pay"
        user_admin.hashed_password = hashed_admin
        print(f"[ATUALIZADO] Admin do Sistema existente atualizado.")

    db.commit()
    print(f" -> Login Admin: {cpf_admin}")
    print(f" -> Senha Admin: {admin_pass}")

    db.close()

if __name__ == "__main__":
    fix_identities()
