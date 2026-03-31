"""
Audit: Bio Code Technology balance inspection.
Reads current balance and recent transactions.
"""
import sys
import os
os.environ['BIO_CODE_TECH_PAY_ALLOWED_START'] = '1'
sys.path.insert(0, '.')

import psycopg
from dotenv import load_dotenv
from urllib.parse import urlparse
import socket

load_dotenv()

DATABASE_URL = os.environ['DATABASE_URL']

# Force IPv4: resolve hostname to A record, use hostaddr for TCP + host for SNI/TLS
_parsed = urlparse(DATABASE_URL)
_hostname = _parsed.hostname
_ipv4 = socket.getaddrinfo(_hostname, None, socket.AF_INET)[0][4][0]

conn = psycopg.connect(
    DATABASE_URL,
    hostaddr=_ipv4,
    connect_timeout=15,
    autocommit=True,
)
cur = conn.cursor()

CNPJ = '61425124000103'
cur.execute(
    "SELECT id, nome, cpf_cnpj, saldo FROM users WHERE cpf_cnpj = %s",
    (CNPJ,)
)
row = cur.fetchone()
if not row:
    print('ERRO: usuario Bio Code Technology nao encontrado')
    sys.exit(1)

user_id, nome, cpf_cnpj, saldo = row
print(f'Usuario : {nome}')
print(f'CNPJ    : {cpf_cnpj}')
print(f'ID      : {user_id}')
print(f'Saldo   : R${float(saldo):.2f}')
print()

cur.execute(
    """
    SELECT id, valor, tipo, status, idempotency_key, descricao, criado_em, taxa_valor
    FROM transacoes_pix
    WHERE user_id = %s
    ORDER BY criado_em DESC
    LIMIT 15
    """,
    (user_id,)
)
rows = cur.fetchall()
if rows:
    print('Ultimas transacoes PIX:')
    print(f'{"Data":<22} {"Tipo":<10} {"Status":<12} {"Valor":>10}  {"Descricao":<45}')
    print('-' * 110)
    for r in rows:
        tx_id, valor, tipo, status, idemp, desc, created, fee = r
        print(f'{str(created):<22} {str(tipo):<10} {str(status):<12} R${float(valor):>8.2f}  {(desc or "")[:44]}')
else:
    print('Nenhuma transacao PIX encontrada.')

cur.close()
conn.close()
