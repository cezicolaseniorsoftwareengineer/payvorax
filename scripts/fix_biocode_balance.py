"""
Audit + correcao de saldo: Bio Code Technology (CNPJ 61425124000103).

Usa psycopg3 com hostaddr IPv4 para evitar hang por DNS IPv6
e manter channel_binding funcional via SNI.

Execucao:
  python scripts\\fix_biocode_balance.py [--dry-run]
"""
import sys, os
sys.path.insert(0, '.')
os.environ['BIO_CODE_TECH_PAY_ALLOWED_START'] = '1'

import psycopg
from psycopg.rows import dict_row
from urllib.parse import urlparse
from decimal import Decimal
from uuid import uuid4
from dotenv import load_dotenv
import socket

load_dotenv()

DRY_RUN      = '--dry-run' in sys.argv
CNPJ         = '61425124000103'
EXPECTED_BAL = Decimal('12.00')
IDEMPOTENCY  = 'reconcil-biocode-excess-20260326'

# --- Resolve IPv4 and connect with hostaddr + host (SNI) ---
raw_url  = os.environ['DATABASE_URL']
_parsed  = urlparse(raw_url)
_ipv4    = socket.getaddrinfo(_parsed.hostname, None, socket.AF_INET)[0][4][0]

print('Conectando ao banco...')
conn = psycopg.connect(
    raw_url,
    hostaddr=_ipv4,
    connect_timeout=15,
    autocommit=False,
    row_factory=dict_row,
)
cur = conn.cursor()

try:
    # --- Buscar usuario ---
    cur.execute(
        "SELECT id, nome, cpf_cnpj, saldo FROM users WHERE cpf_cnpj = %s",
        (CNPJ,)
    )
    row = cur.fetchone()
    if not row:
        print(f'ERRO: usuario com CNPJ {CNPJ} nao encontrado.')
        sys.exit(1)

    user_id = row['id']
    name    = row['nome']
    current = Decimal(str(row['saldo']))
    excess  = current - EXPECTED_BAL

    print(f'Usuario  : {name}')
    print(f'CNPJ     : {CNPJ}')
    print(f'ID       : {user_id}')
    print(f'Saldo    : R${float(current):.2f}')
    print(f'Esperado : R${float(EXPECTED_BAL):.2f}')
    print(f'Excesso  : R${float(excess):.2f}')
    print()

    if abs(excess) < Decimal('0.01'):
        print('Saldo ja esta correto. Nada a fazer.')
        sys.exit(0)

    if excess < 0:
        print(f'AVISO: saldo ABAIXO do esperado (deficit R${float(-excess):.2f}). Abortando.')
        sys.exit(1)

    # --- Idempotency: nao corrigir duas vezes ---
    cur.execute(
        "SELECT id FROM transacoes_pix WHERE idempotency_key = %s",
        (IDEMPOTENCY,)
    )
    if cur.fetchone():
        print(f'Correcao ja aplicada anteriormente. Abortando.')
        sys.exit(0)

    if DRY_RUN:
        print('[DRY-RUN] Nenhuma alteracao aplicada.')
        print(f'  Seria debitado: R${float(excess):.2f}')
        print(f'  Novo saldo seria: R${float(EXPECTED_BAL):.2f}')
        print(f'  tx.idempotency_key: {IDEMPOTENCY}')
        sys.exit(0)

    # --- Criar PixTransaction de estorno ---
    # tipo='ENVIADO' (TransactionType.SENT), status='CONFIRMADO' (PixStatus.CONFIRMED)
    tx_id = str(uuid4())
    cur.execute("""
        INSERT INTO transacoes_pix
            (id, valor, chave_pix, tipo_chave, tipo, status, user_id,
             idempotency_key, descricao, taxa_valor, criado_em, atualizado_em)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
    """, (
        tx_id,
        float(excess),
        'reconciliacao-manual',
        'EVP',
        'ENVIADO',
        'CONFIRMADO',
        user_id,
        IDEMPOTENCY,
        'Estorno excesso saldo - reconciliacao manual 26/03/2026',
        0.0,
    ))

    # --- Atualizar saldo do usuario ---
    cur.execute(
        "UPDATE users SET saldo = %s WHERE id = %s",
        (float(EXPECTED_BAL), user_id)
    )

    conn.commit()
    print(f'OK: saldo corrigido.')
    print(f'  Saldo anterior : R${float(current):.2f}')
    print(f'  Estorno        : R${float(excess):.2f}')
    print(f'  Novo saldo     : R${float(EXPECTED_BAL):.2f}')
    print(f'  TX id          : {tx_id}')

except Exception as e:
    conn.rollback()
    print(f'ERRO: {e}')
    raise
finally:
    cur.close()
    conn.close()
