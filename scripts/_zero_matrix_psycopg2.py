"""
Zera saldo da conta Bio Code Technology (Matrix) via psycopg2 direto.
Usa psycopg2 para evitar o hang do SQLAlchemy SessionLocal com Neon channel_binding.
"""
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import psycopg2

TARGET_CNPJ = "61425124000103"
db_url = os.getenv("DATABASE_URL", "")
if not db_url:
    print("[ERRO] DATABASE_URL nao encontrada no .env")
    sys.exit(1)

try:
    conn = psycopg2.connect(db_url, connect_timeout=10)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(
        "SELECT id, nome, email, saldo FROM users WHERE cpf_cnpj = %s",
        (TARGET_CNPJ,),
    )
    row = cur.fetchone()
    if not row:
        print(f"[ERRO] Conta nao encontrada: CNPJ {TARGET_CNPJ}")
        conn.close()
        sys.exit(1)

    uid, nome, email, saldo = row
    saldo_dec = Decimal(str(saldo))
    print(f"Nome     : {nome}")
    print(f"Email    : {email}")
    print(f"CNPJ     : {TARGET_CNPJ}")
    print(f"Saldo DB : R$ {float(saldo_dec):.2f}")

    if saldo_dec == Decimal("0.00"):
        print("[OK] Saldo ja e zero. Nenhuma alteracao.")
        conn.close()
        sys.exit(0)

    cur.execute(
        "UPDATE users SET saldo = 0.00 WHERE cpf_cnpj = %s",
        (TARGET_CNPJ,),
    )
    conn.commit()

    cur.execute("SELECT saldo FROM users WHERE cpf_cnpj = %s", (TARGET_CNPJ,))
    confirmed = Decimal(str(cur.fetchone()[0]))
    print(f"Saldo apos zeragem: R$ {float(confirmed):.2f}")

    if confirmed == Decimal("0.00"):
        print("[OK] Zeragem concluida com sucesso.")
    else:
        print(f"[ALERTA] Divergencia: esperado 0.00, real {float(confirmed):.2f}")
        sys.exit(2)

    conn.close()

except Exception as exc:
    print(f"[ERRO] {exc}")
    sys.exit(1)
