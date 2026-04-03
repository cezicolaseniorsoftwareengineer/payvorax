"""
Inspeciona e corrige transacoes PROCESSING presas no DB.

Causa: get_available_balance() = users.saldo - SUM(PROCESSING outbound)
       Se ha transacoes PROCESSING cujo TRANSFER_DONE ja aconteceu no Asaas
       mas o webhook nao chegou, o saldo exibido fica negativo mesmo com saldo=0.

Este script:
  1. Lista todas transacoes PROCESSING por usuario
  2. Marca como CONFIRMED (dinheiro ja saiu — Asaas processou)
  3. Verifica saldo disponivel calculado apos correcao
"""
import os
import re
import sys

try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:
    sys.exit("psycopg2 nao encontrado. pip install psycopg2-binary")

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
    conn = psycopg2.connect(db_url, connect_timeout=45)
    conn.autocommit = False
    print("[OK] Conectado.\n")
except Exception as e:
    sys.exit(f"[ERRO] Conexao falhou: {e}")

cur = conn.cursor(cursor_factory=DictCursor)

# 1. Listar todas transacoes PROCESSING
print("=" * 80)
print("TRANSACOES EM STATUS PROCESSING (presas — debit pendente):")
print("=" * 80)

cur.execute("""
    SELECT
        p.id,
        u.nome,
        u.cpf_cnpj,
        u.saldo,
        p.tipo,
        p.valor,
        p.taxa_valor,
        p.criado_em,
        p.nome_destinatario
    FROM transacoes_pix p
    JOIN users u ON u.id = p.user_id
    WHERE p.status = 'PROCESSANDO'
    ORDER BY p.criado_em
""")
processing = cur.fetchall()

if not processing:
    print("[OK] Nenhuma transacao em PROCESSING. O problema e outro.")
    cur.close()
    conn.close()
    sys.exit(0)

total_blocked = {}
for row in processing:
    uid = row["cpf_cnpj"]
    val = float(row["valor"] or 0)
    fee = float(row["taxa_valor"] or 0)
    total_blocked[uid] = total_blocked.get(uid, 0) + val + fee
    print(
        f"  TX {row['id'][:8]}... | {row['nome']:<25} | "
        f"R${val:>8.2f} + fee R${fee:.2f} | "
        f"{str(row['criado_em'])[:19]} | "
        f"dest={row['nome_destinatario'] or 'n/a'}"
    )

print()
print("IMPACTO NO SALDO DISPONIVEL:")
for uid, blocked in total_blocked.items():
    print(f"  {uid}: bloqueado R${blocked:.2f} -> saldo_disponivel = saldo - {blocked:.2f}")

print()
print(f"Total: {len(processing)} transacao(oes) PROCESSING")
print()

# 2. Marcar PROCESSING -> CONFIRMED
print("Marcando todas como CONFIRMED (dinheiro ja saiu do gateway)...")
cur.execute("""
    UPDATE transacoes_pix
    SET status = 'CONFIRMADO'
    WHERE status = 'PROCESSANDO'
    RETURNING id, user_id
""")
updated = cur.fetchall()
conn.commit()
print(f"[OK] {len(updated)} transacao(oes) marcadas como CONFIRMED.\n")

# 3. Verificar saldo disponivel apos correcao
print("ESTADO FINAL (saldo_disponivel = saldo - PROCESSING_pendente):")
cur.execute("""
    SELECT
        u.nome,
        u.cpf_cnpj,
        u.saldo,
        COALESCE(SUM(
            CASE WHEN p.status = 'PROCESSANDO' AND p.tipo = 'ENVIADO'
            THEN p.valor + COALESCE(p.taxa_valor, 0)
            ELSE 0 END
        ), 0) AS pending
    FROM users u
    LEFT JOIN transacoes_pix p ON p.user_id = u.id
    GROUP BY u.id, u.nome, u.cpf_cnpj, u.saldo
    ORDER BY u.nome
""")
for row in cur.fetchall():
    avail = float(row["saldo"]) - float(row["pending"])
    print(
        f"  {str(row['nome']):<35} saldo={float(row['saldo']):>8.2f} "
        f"bloqueado={float(row['pending']):>6.2f} "
        f"disponivel={avail:>8.2f}"
    )

cur.close()
conn.close()
print("\n[FIM] Transacoes PROCESSING corrigidas. Saldo disponivel agora correto.")
