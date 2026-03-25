"""
Simulacao de pagamento via QR Code com chave CNPJ (caso da maquininha).

Fluxo testado:
  1. Constroi payload EMV valido (CNPJ 24.455.140/0001-12, R$ 1,00)
         - Sem payloadLocation URL (QR estatico real)
         - CRC16/CCITT-FALSE calculado na hora
  2. Chama POST /pix/qrcode/consultar  -> espera valor 1.00 + nome merchant
  3. Chama POST /pix/qrcode/pagar      -> sandbox guard: espera HTTP 422
         com mensagem de producao (nao "invalido ou expirado")
  4. Chama /pix/qrcode/consultar com payload corrompido -> espera CRC reject
  5. Relata resultado com evidencia observavel.

Executar de dentro do workspace:
    python scripts/simulate_qrcode_cnpj.py
"""
import sys
import os

# Inject marker BEFORE importing app.main so the startup guard is bypassed.
# The guard allows execution when either pytest or BIO_CODE_TECH_PAY_ALLOWED_START is set.
os.environ["BIO_CODE_TECH_PAY_ALLOWED_START"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.database import Base, get_db
from app.auth.models import User
from app.pix.models import PixTransaction
from app.auth.service import get_password_hash

# ---------------------------------------------------------------------------
# Helpers EMV (copia dos helpers do router para construir payloads realistas)
# ---------------------------------------------------------------------------

def _crc16_ccitt(data: str) -> str:
    crc = 0xFFFF
    for byte in data.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return format(crc, "04X")


def _emv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def build_cnpj_static_qr(cnpj: str, value: float, merchant_name: str, city: str) -> str:
    """
    Constroi QR Code PIX estatico com chave CNPJ — identico ao que maquininhas
    e gateways de pagamento geram para cobrar via chave CNPJ.

    Campo 26 sub-tag 00: GUI = BR.GOV.BCB.PIX
    Campo 26 sub-tag 01: chave PIX (CNPJ no formato com pontos/barra/traco)
    Campo 54: valor em formato decimal "1.00"
    Campo 63: CRC-16/CCITT-FALSE (calculado sobre tudo até "6304" inclusive)
    """
    gui = _emv("00", "BR.GOV.BCB.PIX")
    key = _emv("01", cnpj)
    merchant_account = _emv("26", gui + key)

    amount_str = f"{value:.2f}"
    name_truncated = merchant_name[:25]
    city_truncated = city[:15]

    payload = (
        _emv("00", "01") +
        _emv("01", "12") +          # 12 = multi-use static (maquininha padrao)
        merchant_account +
        _emv("52", "0000") +
        _emv("53", "986") +
        _emv("54", amount_str) +
        _emv("58", "BR") +
        _emv("59", name_truncated) +
        _emv("60", city_truncated) +
        "6304"
    )
    return payload + _crc16_ccitt(payload)


# ---------------------------------------------------------------------------
# Infraestrutura de teste: SQLite in-memory, usuario com saldo
# ---------------------------------------------------------------------------

SQLDB = "sqlite:///:memory:"
engine = create_engine(SQLDB, connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _bootstrap():
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_db
    db = SessionLocal()
    user = User(
        name="Simulador QR CNPJ",
        email="simqrcnpj@biotechpay.com",
        cpf_cnpj="00000000001",
        hashed_password=get_password_hash("Teste@123"),
        balance=500.0,
        credit_limit=0.0,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user


def _get_cookie(client: TestClient, cpf_cnpj: str, password: str) -> str:
    """Returns the raw token string (sem 'Bearer ') para uso em cookies."""
    response = client.post(
        "/auth/login",
        json={"cpf_cnpj": cpf_cnpj, "password": password},
    )
    assert response.status_code == 200, f"Login falhou: {response.text}"
    return response.json()["access_token"]


# ---------------------------------------------------------------------------
# Execucao da simulacao
# ---------------------------------------------------------------------------

def run():
    print()
    print("=" * 65)
    print("SIMULACAO: QR Code com chave CNPJ (maquininha)")
    print("=" * 65)

    _bootstrap()
    client = TestClient(app)
    token = _get_cookie(client, "00000000001", "Teste@123")
    # O sistema usa cookie 'access_token', nao Authorization header
    auth = {"access_token": f"Bearer {token}"}

    # --- Payload real reproduzindo o QR da imagem ---
    cnpj = "24.455.140/0001-12"
    valor = 1.00
    payload = build_cnpj_static_qr(cnpj, valor, "BENEFICIARIO CNPJ", "SAO PAULO")

    print(f"\nPayload EMV construido:")
    print(f"  CNPJ  : {cnpj}")
    print(f"  Valor : R$ {valor:.2f}")
    print(f"  Tamanho payload : {len(payload)} chars")
    print(f"  CRC16 : {payload[-4:]}")
    print(f"  Inicio: {payload[:40]}...")

    # -----------------------------------------------------------------------
    # TESTE 1: consultar — deve resolver Stage 2 (field-54) sem rede
    # -----------------------------------------------------------------------
    print("\n[TESTE 1] POST /pix/qrcode/consultar")
    print("  Esperado: HTTP 200, valor=1.0, sem chamada de rede externa")

    resp = client.post(
        "/pix/qrcode/consultar",
        json={"payload": payload},
        cookies=auth,
    )

    if resp.status_code == 200:
        data = resp.json()
        v = data.get("value")
        nome = data.get("beneficiary_name", "")
        is_int = data.get("is_internal", False)
        resultado = "PASS" if abs(v - 1.0) < 0.001 else "FAIL valor divergente"
        print(f"  Resultado : {resultado}")
        print(f"  value     : R$ {v:.2f}")
        print(f"  beneficiary_name: {nome!r}")
        print(f"  is_internal: {is_int}")
    else:
        print(f"  FAIL — HTTP {resp.status_code}: {resp.text[:300]}")

    # -----------------------------------------------------------------------
    # TESTE 2: pagar em sandbox — deve retornar 422 por sandbox guard,
    # NAO "invalido ou expirado"
    # -----------------------------------------------------------------------
    print("\n[TESTE 2] POST /pix/qrcode/pagar (sandbox guard)")
    print("  Esperado: HTTP 422 com mensagem de producao requerida")

    resp2 = client.post(
        "/pix/qrcode/pagar",
        json={"payload": payload, "description": "Simulacao CNPJ QR"},
        cookies=auth,
    )

    detail = ""
    try:
        detail = resp2.json().get("detail", "")
    except Exception:
        detail = resp2.text

    if resp2.status_code == 422 and "producao" in detail.lower():
        print(f"  Resultado : PASS (sandbox guard ativado corretamente)")
        print(f"  Mensagem  : {detail[:130]}")
    elif resp2.status_code == 422 and ("invalido" in detail.lower() or "expirado" in detail.lower()):
        print(f"  FAIL — ainda retornando 'invalido ou expirado' incorretamente")
        print(f"  Detalhe: {detail[:200]}")
    else:
        print(f"  HTTP {resp2.status_code}: {detail[:200]}")

    # -----------------------------------------------------------------------
    # TESTE 3: payload com CRC corrompido — deve ser rejeitado
    # -----------------------------------------------------------------------
    print("\n[TESTE 3] Payload corrompido (CRC invalido)")
    print("  Esperado: HTTP 422 — checksum incorreto")

    corrupted = payload[:-4] + "0000"   # sobrescreve CRC com zeros
    resp3 = client.post(
        "/pix/qrcode/consultar",
        json={"payload": corrupted},
        cookies=auth,
    )
    if resp3.status_code == 422 and "checksum" in (resp3.json().get("detail") or "").lower():
        print(f"  Resultado : PASS")
    else:
        print(f"  HTTP {resp3.status_code}: {resp3.text[:200]}")

    # -----------------------------------------------------------------------
    # TESTE 4: QR interno valido (BioCodeTechPay nativo) --- deve passar
    # -----------------------------------------------------------------------
    print("\n[TESTE 4] QR interno BioCodeTechPay")
    print("  Esperado: HTTP 200, is_internal=True")

    from app.pix.router import _build_pix_static_emv
    db = SessionLocal()
    user = db.query(User).filter(User.email == "simqrcnpj@biotechpay.com").first()
    from app.pix.models import PixTransaction, PixStatus, TransactionType
    from app.pix.schemas import PixKeyType
    charge_id = str(uuid4())
    charge = PixTransaction(
        id=charge_id,
        value=1.0,
        pix_key=charge_id,
        key_type=PixKeyType.RANDOM.value,
        type=TransactionType.RECEIVED,
        status=PixStatus.CREATED,
        idempotency_key=str(uuid4()),
        user_id=user.id,
        description="Cobranca interna simulacao",
    )
    db.add(charge)
    db.commit()
    db.close()

    internal_emv = _build_pix_static_emv(charge_id, 1.0)
    resp4 = client.post(
        "/pix/qrcode/consultar",
        json={"payload": internal_emv},
        cookies=auth,
    )
    if resp4.status_code == 200 and resp4.json().get("is_internal") is True:
        print(f"  Resultado : PASS — QR interno reconhecido")
        print(f"  value     : R$ {resp4.json()['value']:.2f}")
    else:
        print(f"  HTTP {resp4.status_code}: {resp4.text[:200]}")

    # -----------------------------------------------------------------------
    # SUMARIO
    # -----------------------------------------------------------------------
    print()
    print("=" * 65)
    print("SUMARIO DA SIMULACAO")
    print("=" * 65)
    print("  Teste 1 — consultar CNPJ (Stage 2 field-54) : verificado acima")
    print("  Teste 2 — sandbox guard no pagar            : verificado acima")
    print("  Teste 3 — CRC corrompido rejeitado          : verificado acima")
    print("  Teste 4 — QR interno BioCodeTechPay         : verificado acima")
    print()
    print("Para o pagamento real funcionar em producao:")
    print("  Render Dashboard -> ASAAS_USE_SANDBOX=False")
    print("  Render Dashboard -> ASAAS_API_KEY=$aact_prod_...")
    print()


if __name__ == "__main__":
    run()
