"""
Migration: add CHECK constraint saldo >= 0 on users table.

Steps:
  1. Clamp all negative saldo to 0.00 (idempotent).
  2. Drop constraint if already exists (idempotent re-run).
  3. Add CHECK (saldo >= 0) constraint.
  4. Verify constraint is active.

Run once per environment. Safe to re-run.
"""
import os
import re
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 nao encontrado. pip install psycopg2-binary")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CONSTRAINT_NAME = "chk_users_saldo_non_negative"

db_url = os.environ.get("DATABASE_URL", "")
if not db_url:
    sys.exit("DATABASE_URL nao configurado.")

db_url = re.sub(r"\?.*", "", db_url)
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

print("Conectando ao banco...")
try:
    conn = psycopg2.connect(db_url, connect_timeout=45)
    conn.autocommit = False
    print("[OK] Conectado.")
except Exception as e:
    sys.exit(f"[ERRO] Conexao falhou: {e}")

cur = conn.cursor()

# 1. Clamp all negative balances to 0
cur.execute("UPDATE users SET saldo = 0.00 WHERE saldo < 0 RETURNING nome, cpf_cnpj, saldo")
clamped = cur.fetchall()
if clamped:
    print(f"[CLAMP] {len(clamped)} conta(s) negativas zeradas antes de adicionar constraint:")
    for row in clamped:
        print(f"  {row[0]} ({row[1]}) -> R$ 0.00")
else:
    print("[OK] Nenhum saldo negativo encontrado.")

# 2. Drop old constraint if exists (idempotent)
cur.execute(
    """
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = %s AND table_name = 'users'
    """,
    (CONSTRAINT_NAME,),
)
exists = cur.fetchone()
if exists:
    cur.execute(f"ALTER TABLE users DROP CONSTRAINT {CONSTRAINT_NAME}")
    print(f"[DROP] Constraint existente '{CONSTRAINT_NAME}' removida (re-run idempotente).")

# 3. Add CHECK constraint
cur.execute(
    f"ALTER TABLE users ADD CONSTRAINT {CONSTRAINT_NAME} CHECK (saldo >= 0)"
)
print(f"[ADD] Constraint '{CONSTRAINT_NAME}' adicionada: CHECK (saldo >= 0)")

conn.commit()
print("[COMMIT] Alteracoes salvas.")

# 4. Verify
cur.execute(
    """
    SELECT constraint_name, check_clause
    FROM information_schema.check_constraints
    WHERE constraint_name = %s
    """,
    (CONSTRAINT_NAME,),
)
row = cur.fetchone()
if row:
    print(f"[VERIFICADO] Constraint ativa: {row[0]} — {row[1]}")
else:
    print("[ERRO] Constraint nao encontrada apos adicao. Verificar manualmente.")
    sys.exit(1)

# 5. Final balance state
cur.execute("SELECT nome, cpf_cnpj, saldo FROM users ORDER BY saldo")
print("\nEstado final dos saldos:")
for r in cur.fetchall():
    print(f"  {str(r[0]):<35} {str(r[1]):<20} R$ {float(r[2]):>8.2f}")

cur.close()
conn.close()
print("\n[FIM] Constraint aplicada com sucesso. Saldo negativo agora e impossivel no DB.")
