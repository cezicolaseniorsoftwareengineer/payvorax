"""
Zeragem cirurgica de saldos via psycopg2 direto.
Regras:
  - Karen (cpf=22317665822): manter R$10.00 — nao alterar
  - Todos os demais: setar para 0.00
Script idempotente, com verificacao pre e pos, audit print.
"""
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import psycopg2

KAREN_CPF = "22317665822"
KAREN_EXPECTED = Decimal("10.00")

db_url = os.getenv("DATABASE_URL", "")
if not db_url:
    print("[ERRO] DATABASE_URL nao encontrada no .env")
    sys.exit(1)

try:
    conn = psycopg2.connect(db_url, connect_timeout=10)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("SELECT id, nome, email, cpf_cnpj, saldo FROM users ORDER BY nome")
    rows = cur.fetchall()

    print(f"{'NOME':<30} {'CPF/CNPJ':<20} {'SALDO ANTES':>12} {'ACAO'}")
    print("-" * 75)

    changed = 0
    for uid, nome, email, cpf_cnpj, saldo in rows:
        saldo_dec = Decimal(str(saldo)).quantize(Decimal("0.01"))
        raw_cpf = "".join(c for c in (cpf_cnpj or "") if c.isdigit())

        if raw_cpf == KAREN_CPF:
            target = KAREN_EXPECTED
            action = "MANTER R$10.00" if saldo_dec == KAREN_EXPECTED else f"CORRIGIR -> R$10.00"
        else:
            target = Decimal("0.00")
            action = "ja zerado" if saldo_dec == Decimal("0.00") else "ZERAR"

        print(f"  {nome:<28} {raw_cpf:<20} R$ {float(saldo_dec):>8.2f}   {action}")

        if saldo_dec != target:
            cur.execute(
                "UPDATE users SET saldo = %s WHERE id = %s",
                (float(target), uid),
            )
            changed += 1

    if changed:
        conn.commit()
        print(f"\n[OK] {changed} conta(s) atualizada(s). Verificando...")
        cur.execute("SELECT nome, cpf_cnpj, saldo FROM users ORDER BY nome")
        post_rows = cur.fetchall()
        print(f"\n{'NOME':<30} {'SALDO APOS':>12}")
        print("-" * 44)
        ok = True
        for nome, cpf_cnpj, saldo in post_rows:
            raw_cpf = "".join(c for c in (cpf_cnpj or "") if c.isdigit())
            expected = KAREN_EXPECTED if raw_cpf == KAREN_CPF else Decimal("0.00")
            confirmed = Decimal(str(saldo)).quantize(Decimal("0.01"))
            status = "OK" if confirmed == expected else f"DIVERGENCIA esperado={expected}"
            if confirmed != expected:
                ok = False
            print(f"  {nome:<28} R$ {float(confirmed):>8.2f}   {status}")
        if ok:
            print("\n[OK] Todos os saldos corretos.")
        else:
            print("\n[ALERTA] Divergencias detectadas — revisar manualmente.")
            sys.exit(2)
    else:
        print("\n[OK] Nenhuma alteracao necessaria — todos os saldos ja corretos.")

    conn.close()

except Exception as exc:
    print(f"[ERRO] {exc}")
    import traceback; traceback.print_exc()
    sys.exit(1)
