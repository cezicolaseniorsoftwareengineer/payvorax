"""
Diagnóstico e correção de saldos negativos — execução direta.
"""
import os
import re
import sys
from decimal import Decimal

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 nao encontrado. Instale: pip install psycopg2-binary")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

db_url = os.environ.get("DATABASE_URL", "")
if not db_url:
    sys.exit("DATABASE_URL nao configurado.")

db_url = re.sub(r"\?.*", "", db_url)
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

print("Conectando ao banco...")
try:
    conn = psycopg2.connect(db_url, connect_timeout=20)
    conn.autocommit = False
    print("[OK] Conectado.")
except Exception as e:
    sys.exit(f"[ERRO] Conexao falhou: {e}")

cur = conn.cursor()

# Leitura atual
cur.execute("SELECT id, nome, cpf_cnpj, saldo FROM users ORDER BY saldo")
rows = cur.fetchall()

print(f"\n{'NOME':<35} {'CPF/CNPJ':<20} {'SALDO':>12}  STATUS")
print("-" * 80)
negative = []
for row in rows:
    uid, name, doc, saldo = row
    saldo_f = float(saldo)
    flag = "  <<< NEGATIVO" if saldo_f < 0 else ""
    print(f"{str(name):<35} {str(doc):<20} R${saldo_f:>10.2f}{flag}")
    if saldo_f < 0:
        negative.append((uid, name, doc))

if not negative:
    print("\n[OK] Nenhuma conta com saldo negativo.")
    cur.close()
    conn.close()
    sys.exit(0)

print(f"\n[ACAO] {len(negative)} conta(s) com saldo negativo. Zerando...")

# Define contas protegidas que devem manter saldo especifico
KEEP_KAREN_CPF = "22317665822"

for uid, name, doc in negative:
    raw_doc = re.sub(r"\D", "", str(doc))
    if raw_doc == KEEP_KAREN_CPF:
        print(f"  [SKIP] {name} — conta Karen protegida")
        continue
    cur.execute(
        "UPDATE users SET saldo = 0.00 WHERE id = %s",
        (uid,)
    )
    print(f"  [ZERADO] {name} (id={uid})")

conn.commit()
print("\n[COMMIT] Alteracoes salvas.")

# Verificacao pos-update
cur.execute("SELECT nome, cpf_cnpj, saldo FROM users ORDER BY saldo")
after = cur.fetchall()
print(f"\n{'NOME':<35} {'CPF/CNPJ':<20} {'SALDO':>12}")
print("-" * 70)
for r in after:
    flag = "  <<< AINDA NEGATIVO" if float(r[2]) < 0 else ""
    print(f"{str(r[0]):<35} {str(r[1]):<20} R${float(r[2]):>10.2f}{flag}")

cur.close()
conn.close()
print("\n[FIM] Diagnostico concluido.")
