"""
FastAPI Router for PIX endpoints.
Exposes RESTful API with strict validation and automated documentation.
"""
import hmac
import re
import httpx
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session

from app.pix.models import TransactionType, PixTransaction
from app.pix.schemas import (
    PixCreateRequest,
    PixConfirmRequest,
    PixResponse,
    PixStatementResponse,
    PixChargeRequest,
    PixChargeResponse,
    PixChargeConfirmRequest,
    PixQrCodePayRequest,
    PixQrCodeConsultarRequest,
    PixStatus,
    PixKeyType
)
from app.pix.service import create_pix, confirm_pix, get_pix, list_statement, cancel_pix, ensure_asaas_customer, credit_pix_receipt
from app.pix.internal_transfer import find_recipient_user
from app.adapters.gateway_factory import get_payment_gateway
from decimal import Decimal
from datetime import datetime as _dt, date as _date, timedelta as _td
from app.core.database import get_db
from app.core.logger import get_logger_with_correlation, audit_log
from app.auth.dependencies import get_current_user, require_active_account
from app.auth.models import User
from app.core.utils import mask_cpf_cnpj, format_brasilia_time
from app.core.fees import calculate_pix_fee, fee_display, is_pj
from app.core.pix_emv import build_pix_static_emv as _build_pix_static_emv, build_qr_url as _build_qr_url
from app.core.config import settings as _settings
from app.antifraude.rules import antifraud_engine as _antifraud_engine
from app.antifraude.schemas import AntifraudTransaction as _AntifraudTx

router = APIRouter(tags=["PIX"])

# ---------------------------------------------------------------------------
# Platform PIX receiving key (Asaas account EVP key registered in BACEN DICT).
# Single shared deposit wallet. All inbound PIX — via virtual keys, CPF/CNPJ key
# type, or direct self-deposit — arrive at this wallet. The webhook resolves the
# recipient by: (1) pix_random_key / pix_email_key, (2) CPF/CNPJ key match,
# (3) sender CPF/CNPJ self-deposit identification.
# Override via PLATFORM_PIX_KEY env var (Render Dashboard).
# Run `python scripts/check_pix_key.py` to discover the correct key for your Asaas account.
# ---------------------------------------------------------------------------
_FALLBACK_PLATFORM_KEY = "1a923d7b-3230-46d4-a670-87bf7ee54817"
_PLATFORM_PIX_KEY: str = _settings.PLATFORM_PIX_KEY or _FALLBACK_PLATFORM_KEY
_SHARED_DEPOSIT_WALLET_ID: str = _PLATFORM_PIX_KEY

# ---------------------------------------------------------------------------
# Module-level helpers shared by /qrcode/consultar and /qrcode/pagar
# ---------------------------------------------------------------------------
_UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE
)
_ASAAS_ID_RE = re.compile(r'pay_[A-Za-z0-9]+')


# ---------------------------------------------------------------------------
# BR Code PIX EMV helpers (BACEN spec — ABECS ISO 18004)
# Implementations live in app.core.pix_emv — kept here as thin wrappers
# for the parse/extract helpers that are only used by this router.
# ---------------------------------------------------------------------------

def _crc16_ccitt(data: str) -> str:
    """
    CRC-16/CCITT-FALSE (polynomial 0x1021, init 0xFFFF).
    Required by BACEN BR Code PIX specification (section 4.1).
    Mandatory for interoperability — any PSP app validates this before even querying DICT.
    """
    crc = 0xFFFF
    for byte in data.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return format(crc, "04X")


def _emv(tag: str, value: str) -> str:
    """Encodes a single TLV field: tag(2) + length(2, zero-padded decimal) + value."""
    return f"{tag}{len(value):02d}{value}"


def _build_pix_static_emv(charge_id: str, value: float) -> str:
    # Delegates to canonical implementation in app.core.pix_emv (imported above).
    # This wrapper is kept for call-site compatibility within this module.
    from app.core.pix_emv import build_pix_static_emv as _canonical
    return _canonical(charge_id, value)


def _parse_emv_top_level(emv: str) -> dict:
    """
    Walk the top-level TLV fields of a BR Code PIX EMV string sequentially.

    Using re.search() to find tag 54 anywhere in the string causes false-positive
    matches: for example, MCC field 52 may carry value 5411 (supermarkets), and
    the regex matches '5411' as 'field=54, length=11', reading garbage.

    Sequential TLV traversal reads tag+length+value one at a time from position 0,
    ensuring only true top-level boundaries are parsed.
    """
    fields: dict = {}
    pos = 0
    while pos + 4 <= len(emv):
        tag = emv[pos:pos + 2]
        if tag == "63":
            # CRC tag — terminal field, always last 8 chars. Stop here.
            break
        try:
            length = int(emv[pos + 2:pos + 4])
        except ValueError:
            break
        end = pos + 4 + length
        if end > len(emv):
            break
        fields[tag] = emv[pos + 4:end]
        pos = end
    return fields


def _parse_emv_value(emv: str) -> float:
    """Extract Transaction Amount from BR PIX EMV field 54 (BRL decimal string)."""
    raw = _parse_emv_top_level(emv.strip()).get("54", "")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 0.0


def _extract_emv_merchant(emv: str) -> str:
    """Extract Merchant Name from BR PIX EMV field 59."""
    return _parse_emv_top_level(emv.strip()).get("59", "").strip()


def _screen_antifraud(
    value: float, user_id: str, db: Session, logger, tx_type: str = "PIX"
) -> None:
    """
    Run the anti-fraud engine against an outgoing transaction.
    Raises HTTPException 403 if the transaction is rejected.
    """
    now = _dt.now()
    since_24h = now - _td(hours=24)
    attempts_24h = db.query(PixTransaction).filter(
        PixTransaction.user_id == user_id,
        PixTransaction.type == TransactionType.SENT,
        PixTransaction.created_at >= since_24h,
    ).count()

    fraud_tx = _AntifraudTx(
        value=value,
        time=now.strftime("%H:%M"),
        attempts_last_24h=attempts_24h,
        transaction_type=tx_type,
    )
    result = _antifraud_engine.analyze(fraud_tx)

    if not result["approved"]:
        logger.warning(
            f"Antifraud REJECTED: user={user_id}, value={value}, "
            f"score={result['score']}, rules={result['triggered_rules']}"
        )
        audit_log(
            action="ANTIFRAUD_REJECTED",
            user=user_id,
            resource=f"value={value}",
            details={
                "score": result["score"],
                "risk_level": result["risk_level"],
                "triggered_rules": result["triggered_rules"],
            },
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"Transacao bloqueada pela analise de risco. "
                f"Score: {result['score']}/100. {result['recommendation']}"
            ),
        )

    logger.info(
        f"Antifraud APPROVED: user={user_id}, value={value}, "
        f"score={result['score']}, level={result['risk_level']}"
    )


def _validate_pix_crc(payload: str) -> bool:
    """
    Validate BR Code CRC-16/CCITT-FALSE (field 63).

    BACEN spec: the payload string up to and including "6304" is hashed; the
    4-hex result appended at position len-4 must match.
    Returns True when valid, False when checksum fails.
    Payloads without a "63" terminal field pass vacuously (internal / test QR).
    """
    emv = payload.strip()
    idx = emv.rfind("6304")
    if idx == -1:
        return True  # no CRC field — treat as valid (internal / test)
    body = emv[:idx + 4]     # everything up to and including "6304"
    expected = _crc16_ccitt(body)
    actual = emv[idx + 4:idx + 8].upper()
    return actual == expected


def _extract_txid_field62(emv: str) -> Optional[str]:
    """
    Extract the txid from BR Code EMV field 62 (Additional Data Field Template),
    sub-tag 05.

    The txid uniquely identifies the charge at the SPI. BACEN spec limits it to
    25 alphanumeric chars (hyphens stripped). All major POS terminals (Stone, Cielo,
    Rede, PagSeguro, Mercado Pago) embed it here.

    Returns the raw txid string, or None when absent.
    """
    additional_raw = _parse_emv_top_level(emv.strip()).get("62", "")
    if not additional_raw:
        return None
    pos = 0
    while pos + 4 <= len(additional_raw):
        tag = additional_raw[pos:pos + 2]
        try:
            length = int(additional_raw[pos + 2:pos + 4])
        except ValueError:
            break
        end = pos + 4 + length
        if end > len(additional_raw):
            break
        if tag == "05":
            return additional_raw[pos + 4:end].strip() or None
        pos = end
    return None


class _PixChargeExpired(Exception):
    """Raised by _fetch_pix_charge_url when the PSP confirms the charge is expired/removed."""


def _extract_pix_url(emv: str) -> Optional[str]:
    """
    Extract the PIX payloadLocation URL from a BR Code EMV string.

    BACEN Manual BR Code v2.1 allows multiple Merchant Account Info fields
    (tags 26–51). The correct one is identified by GUI = "BR.GOV.BCB.PIX"
    in sub-tag 00. Other tags (VISA, MASTERCARD) coexist on multi-network POS
    terminals and MUST be skipped.

    Sub-tag layout:
      00  GUI identifier  ("BR.GOV.BCB.PIX")
      01  PIX key OR payloadLocation URL (PagSeguro, Mercado Pago older firmware)
      25  payloadLocation URL (BACEN canonical position)

    URL normalisation:
      pix://   -> https://
      http://  -> kept as-is
      no scheme -> https:// prepended
    """
    fields = _parse_emv_top_level(emv.strip())

    def _normalise(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("pix://"):
            return "https://" + raw[6:]
        if not raw.startswith("http"):
            return "https://" + raw
        return raw

    def _walk_sub_tlv(raw: str) -> dict:
        sub: dict = {}
        pos = 0
        while pos + 4 <= len(raw):
            tag = raw[pos:pos + 2]
            try:
                length = int(raw[pos + 2:pos + 4])
            except ValueError:
                break
            end = pos + 4 + length
            if end > len(raw):
                break
            sub[tag] = raw[pos + 4:end]
            pos = end
        return sub

    # Scan fields 26–51 (BACEN allows multiple Merchant Account Info blocks)
    # Stop at first block whose GUI is BR.GOV.BCB.PIX
    for field_id in range(26, 52):
        tag = f"{field_id:02d}"
        raw = fields.get(tag, "")
        if not raw:
            continue
        sub = _walk_sub_tlv(raw)
        gui = sub.get("00", "").upper()
        if "BCB.PIX" not in gui and "BR.GOV.BCB" not in gui:
            continue  # not a PIX block — skip (VISA, MASTERCARD, etc.)

        # Sub-tag 25: canonical payloadLocation (BACEN spec)
        val25 = sub.get("25", "")
        if val25 and "/" in val25:
            return _normalise(val25)

        # Sub-tag 01: may hold a PIX key (CNPJ "24.455.140/0001-12", CPF, email,
        # phone, EVP UUID) OR a payloadLocation URL (PagSeguro / Mercado Pago old
        # firmware). CNPJ keys contain "/" and "." but are NOT URLs.
        # Only treat as URL when there is an explicit scheme (https://, http://,
        # pix://) OR when the value starts with a letter-based domain name.
        val01 = sub.get("01", "")
        if val01 and (
            val01.startswith(("https://", "http://", "pix://"))
            or (
                "/" in val01
                and val01[0].isalpha()
                and re.search(r'^[a-zA-Z][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/', val01)
            )
        ):
            return _normalise(val01)

    return None


def _fetch_pix_charge_url(url: str) -> Dict[str, Any]:
    """
    Fetch PIX charge data from the PSP payloadLocation URL (BACEN standard).
    Retries with exponential backoff on transient network errors.

    BACEN COB/COBV JSON schema:
      {
        "txid":   "...",
        "status": "ATIVA" | "EXPIRADA" | "REMOVIDA_PELO_USUARIO_RECEBEDOR" | "CONCLUIDA",
        "valor":  { "original": "10.00" },
        "devedor": { "nome": "...", "cpf": "...", "cnpj": "..." },
        "solicitacaoPagador": "...",
        "pixCopiaECola": "000201..."  (optional — canonical EMV registered at SPI)
      }

    Returns:
        dict with value (float), beneficiary_name (str), txid (Optional[str]),
        and pix_copia_e_cola (Optional[str]).

    Raises:
        _PixChargeExpired: When PSP confirms a terminal status.
        Exception: On network / timeout / parse errors (caller falls through to field-54).
    """
    import json as _json_fetch
    import base64 as _b64_fetch
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
        reraise=True,
    )
    def _do_fetch(target_url: str):
        with httpx.Client(
            timeout=httpx.Timeout(5.0, connect=3.0),
            follow_redirects=True,
        ) as client:
            return client.get(
                target_url,
                headers={
                    "Accept": "application/json, */*",
                    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/121.0.0.0 Mobile Safari/537.36"
                    ),
                    "Cache-Control": "no-cache",
                }
            )

    response = _do_fetch(url)
    # 404 / 410: charge was cleaned up or explicitly expired at the PSP.
    if response.status_code in (404, 410):
        raise _PixChargeExpired(f"HTTP {response.status_code}")
    response.raise_for_status()

    # Handle JWS (JSON Web Signature) responses — BACEN PIX API spec v2.4
    # allows Content-Type: application/jose (PagSeguro, some Santander integrations).
    _resp_text = response.text.strip() if response.content else ""
    _ct = response.headers.get("content-type", "").lower()

    if not _resp_text:
        raise ValueError("payloadLocation response is empty")

    if "jose" in _ct or (not _resp_text.startswith("{") and _resp_text.count(".") >= 2):
        _jws_parts = _resp_text.split(".")
        if len(_jws_parts) < 3:
            raise ValueError("payloadLocation returned invalid JWS format")
        _payload_b64 = _jws_parts[1]
        _pad = 4 - len(_payload_b64) % 4
        if _pad != 4:
            _payload_b64 += "=" * _pad
        data = _json_fetch.loads(_b64_fetch.urlsafe_b64decode(_payload_b64))
    else:
        data = _json_fetch.loads(_resp_text)

    status = (data.get("status") or "").upper()
    _terminal = {"EXPIRADA", "REMOVIDA_PELO_USUARIO_RECEBEDOR", "REMOVIDA_PELO_PSP", "CONCLUIDA"}
    if status in _terminal:
        raise _PixChargeExpired(status)

    raw_value = data.get("valor", {}).get("original")
    if raw_value is None:
        raw_value = data.get("value") or data.get("amount")
    if raw_value is None:
        raise ValueError(f"payloadLocation response has no recognisable value field: {list(data.keys())}")

    devedor = data.get("devedor") or {}
    beneficiary = (
        devedor.get("nome")
        or devedor.get("name")
        or data.get("solicitacaoPagador", "")[:60]
        or data.get("description", "")
        or "Beneficiario"
    )

    # pixCopiaECola: canonical EMV string from the PSP, registered at SPI.
    # When present, re-submitting this to Asaas /pix/qrCodes/pay has the highest
    # acceptance rate because it is the PSP's own official payload.
    pix_copia_e_cola = (data.get("pixCopiaECola") or "").strip() or None

    return {
        "value": float(raw_value),
        "beneficiary_name": beneficiary.strip() or "Beneficiario",
        "txid": (data.get("txid") or "").strip() or None,
        "pix_copia_e_cola": pix_copia_e_cola,
    }


def _find_internal_qrcode_charge(payload: str, db, logger) -> tuple:
    """
    Detects whether a PIX QR Code payload matches an internal BioCodeTechPay charge.
    Tests four routes in priority order:
      1a. UUID scan           — simulation charges embed charge UUID in EMV
      1b. Asaas pay_xxx scan  — charges with pay_xxx ID in the EMV URL
      1c. pix_key exact match — Asaas stores full EMV as pix_key for cobr charges
      1d. cobv UUID LIKE      — Asaas cobv QR codes: UUID inside pix.asaas.com URL
    Returns (PixTransaction | None, is_already_paid: bool).
    """
    # 1a: simulation charges
    for candidate_id in _UUID_RE.findall(payload):
        charge = db.query(PixTransaction).filter(
            PixTransaction.id == candidate_id,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.status == PixStatus.CREATED
        ).first()
        if charge:
            return charge, False

    # 1b: Asaas pay_xxx ID in EMV
    for candidate_id in _ASAAS_ID_RE.findall(payload):
        charge = db.query(PixTransaction).filter(
            PixTransaction.id == candidate_id,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.status == PixStatus.CREATED
        ).first()
        if charge:
            logger.info(f"Route 1b: Asaas charge matched internally: id={candidate_id}")
            return charge, False

    # 1c: pix_key exact match
    pix_key_lookup = payload[:200]
    if pix_key_lookup:
        charge = db.query(PixTransaction).filter(
            PixTransaction.pix_key == pix_key_lookup,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.status == PixStatus.CREATED
        ).first()
        if charge:
            return charge, False

    # 1d: cobv UUID LIKE — pix.asaas.com/qr/cobv/UUID → UUID is in stored pix_key (full EMV)
    # INDEX HINT: For PostgreSQL, create a trigram index to accelerate LIKE '%...%' queries:
    #   CREATE INDEX idx_pix_key_trgm ON pix_transactions USING gin (pix_key gin_trgm_ops);
    #   Requires: CREATE EXTENSION IF NOT EXISTS pg_trgm;
    if "asaas.com" in payload:
        for candidate_uuid in _UUID_RE.findall(payload):
            charge = db.query(PixTransaction).filter(
                PixTransaction.pix_key.like(f"%{candidate_uuid}%"),
                PixTransaction.type == TransactionType.RECEIVED,
                PixTransaction.status == PixStatus.CREATED
            ).first()
            if charge:
                logger.info(f"Route 1d: cobv UUID match in pix_key: id={charge.id}")
                return charge, False

    # Guard: detect already-paid charges (return 409 instead of routing externally)
    all_candidates = list(_UUID_RE.findall(payload)) + list(_ASAAS_ID_RE.findall(payload))
    for candidate_id in all_candidates:
        already_paid = db.query(PixTransaction).filter(
            PixTransaction.id == candidate_id,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.status == PixStatus.CONFIRMED
        ).first()
        if already_paid:
            return None, True

    if "asaas.com" in payload:
        for candidate_uuid in _UUID_RE.findall(payload):
            already_paid = db.query(PixTransaction).filter(
                PixTransaction.pix_key.like(f"%{candidate_uuid}%"),
                PixTransaction.type == TransactionType.RECEIVED,
                PixTransaction.status == PixStatus.CONFIRMED
            ).first()
            if already_paid:
                return None, True

    return None, False


@router.post("/transacoes", response_model=PixResponse, status_code=201)
def create_pix_transaction(
    data: PixCreateRequest,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Creates a new transaction with idempotency support.
    **Requires active account (at least one deposit made) for outgoing transfers, except for self-deposits.**

    - **value**: Transaction value (R$)
    - **key_type**: Key Type (CPF, EMAIL, PHONE, RANDOM)
    - **pix_key**: Valid destination key
    - **X-Idempotency-Key**: Mandatory header to ensure uniqueness

    **Returns:**
    - Transaction metadata and initial state
    """
    # Generate correlation_id for traceability
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    # Enforce Active Account Policy manually, but allow Self-Deposit (Copia e Cola)
    # This allows new users to fund their account via "Pix Copia e Cola" of their own charge.
    has_deposit = db.query(PixTransaction).filter(
        PixTransaction.user_id == current_user.id,
        PixTransaction.type == TransactionType.RECEIVED,
        PixTransaction.status == PixStatus.CONFIRMED
    ).first()

    # Also consider users with positive balance as active accounts.
    # Balance can be positive via admin credits or Asaas webhook confirmations that
    # pre-date transaction-level tracking. The balance invariant is the source of
    # truth for financial capacity; the CONFIRMED RECEIVED check is a secondary
    # activation signal to prevent unactivated spam accounts from sending.
    account_is_active = has_deposit or current_user.balance > 0

    if not account_is_active:
        # If no deposit and no balance, only allow if it looks like a Copia e Cola (potential self-deposit)
        # The service layer will validate if it is indeed a self-deposit and handle it.
        # If it is NOT a self-deposit, the service will check balance (which is 0) and fail safely.
        if not (data.key_type == PixKeyType.RANDOM and len(data.pix_key) > 36):
             raise HTTPException(
                status_code=403,
                detail="Inactive account. Make a first deposit (Received PIX) to unlock all features."
            )

    try:
        logger.info(f"Starting PIX creation: value={data.value} key_type={data.key_type} user={current_user.id}")

        _screen_antifraud(data.value, current_user.id, db, logger, tx_type="PIX_SEND")

        pix = create_pix(
            db,
            data,
            x_idempotency_key,
            correlation_id,
            user_id=current_user.id,
            type=TransactionType.SENT
        )

        # Auto-confirm immediate transactions (Simulating instant payment)
        if pix.status == PixStatus.CREATED and pix.type == TransactionType.SENT:
            confirmed_pix = confirm_pix(db, pix.id, correlation_id)
            if confirmed_pix:
                pix = confirmed_pix

        return build_pix_response(pix, db)

    except ValueError as e:
        logger.warning(f"PIX validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error processing PIX")


@router.post("/transacoes/confirmar", response_model=PixResponse)
def confirm_pix_transaction(
    data: PixConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Confirms a pending transaction.
    Simulates Payment Service Provider (PSP) callback.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        logger.info(f"Confirming PIX: {data.pix_id}")

        pix = confirm_pix(db, data.pix_id, correlation_id)

        if not pix:
            raise HTTPException(status_code=404, detail="Transaction not found")

        return build_pix_response(pix, db)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error confirming PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error confirming transaction")


@router.get("/fee-preview")
def get_pix_fee_preview(
    amount: float,
    is_received: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Returns a real-time fee breakdown for a given amount and direction.

    Used by the UI to display the exact platform fee before the user confirms
    a transfer or creates a charge. Delegates entirely to fee_breakdown() so
    the UI and service layer are always in sync.

    Query params:
      amount      : transaction value in BRL (e.g. 150.00)
      is_received : true for incoming charge preview; false (default) for outbound
    """
    if amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be greater than zero")

    from app.core.fees import fee_breakdown, minimum_viable_outbound_amount, is_pj

    breakdown = fee_breakdown(
        current_user.cpf_cnpj,
        amount,
        is_external=True,
        is_received=is_received,
    )

    result = {
        "amount":        round(amount, 2),
        "fee":           float(breakdown["platform_fee"]),
        "fee_display":   breakdown["fee_display"],
        "fee_label":     breakdown["fee_label"],
        "network_fee":   float(breakdown["network_fee"]),
        "service_fee":   float(breakdown["service_fee"]),
        "net_margin":    float(breakdown["net_margin"]),
        "gateway_cost":  float(breakdown["gateway_cost"]),
        "is_zero_cost":  breakdown["is_zero_cost"],
        "account_type":  "PJ" if is_pj(current_user.cpf_cnpj) else "PF",
    }

    if not is_received and not is_pj(current_user.cpf_cnpj):
        # Warn PJ clients when amount is below break-even threshold
        min_viable = minimum_viable_outbound_amount(current_user.cpf_cnpj)
        result["minimum_viable_amount"] = float(min_viable)
        result["below_minimum"] = amount < float(min_viable)

    return result


@router.get("/transacoes/{pix_id}", response_model=PixResponse)
def get_pix_transaction(
    pix_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Retrieves transaction details by ID.
    For RECEIVED + CREATED charges, performs a lazy status refresh against Asaas
    so the polling loop in the frontend receives an accurate status without needing
    a separate verify endpoint.
    """
    pix = get_pix(db, pix_id, current_user.id)

    if not pix:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Lazy status refresh: if this is an unconfirmed real Asaas charge, ask Asaas now.
    # The check is idempotent — once CONFIRMED it short-circuits immediately.
    if pix.type == TransactionType.RECEIVED and pix.status == PixStatus.CREATED:
        correlation_id = x_correlation_id or str(uuid4())
        logger = get_logger_with_correlation(correlation_id)
        gateway = get_payment_gateway()
        if gateway:
            try:
                charge_status = gateway.get_charge_status(pix_id)
                logger.info(
                    f"Lazy status refresh: charge={pix_id}, "
                    f"asaas_status={charge_status.get('status')}"
                )
                if charge_status.get("status") == "CONFIRMED":
                    pix.status = PixStatus.CONFIRMED
                    # Persist payer name when available — resolves "External Sender" in history
                    payer_info = charge_status.get("payer_info") or {}
                    if payer_info.get("name") and not pix.recipient_name:
                        pix.recipient_name = payer_info["name"]
                    db.add(pix)
                    receiver_user = db.query(User).filter(User.id == pix.user_id).first()
                    if receiver_user:
                        credit_pix_receipt(
                            db, receiver_user, float(pix.value),
                            source=f"lazy_status_refresh:charge_id={pix_id}",
                        )
                    db.commit()
                    db.refresh(pix)
            except Exception as e:
                logger.warning(f"Lazy status refresh failed for {pix_id}: {e}")
                # Non-fatal: return current DB state

    return build_pix_response(pix, db)


@router.delete("/transacoes/{pix_id}", response_model=PixResponse)
def cancel_pix_scheduling(
    pix_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Cancels a scheduled transaction.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        logger.info(f"PIX cancellation request: {pix_id} user={current_user.id}")

        pix = cancel_pix(db, pix_id, current_user.id, correlation_id)

        if not pix:
            raise HTTPException(status_code=404, detail="Transaction not found or does not belong to user")

        return build_pix_response(pix, db)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error cancelling PIX: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error cancelling transaction")


@router.get("/extrato", response_model=PixStatementResponse)
def get_statement(
    status: Optional[PixStatus] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> PixStatementResponse:
    """
    Retrieves transaction ledger with optional status filtering.
    Optimized with batch loading to prevent N+1 query issues.
    """
    result: Dict[str, Any] = list_statement(db, current_user.id, limit, status.value if status else None)

    transactions = result["transactions"]

    # --- BATCH LOADING OPTIMIZATION ---
    # 1. Collect all relevant IDs
    user_ids = {t.user_id for t in transactions}
    correlation_ids = {t.correlation_id for t in transactions if t.correlation_id}

    # 2. Fetch all related Users in one query
    # We need the current user (already have) and potentially others if we were admin,
    # but here we mostly need the current user.
    # However, for internal transfers, we need the OTHER user.
    # The 'build_pix_response' logic looks for related transactions to find the other user.

    # 3. Fetch all related Transactions (Counterparts) in one query
    related_txs = []
    if correlation_ids:
        related_txs = db.query(PixTransaction).filter(
            PixTransaction.correlation_id.in_(correlation_ids),
            PixTransaction.id.notin_([t.id for t in transactions]) # Exclude self
        ).all()

    # Map correlation_id -> related_transaction
    related_tx_map = {tx.correlation_id: tx for tx in related_txs}

    # 4. Collect User IDs from related transactions
    related_user_ids = {tx.user_id for tx in related_txs}
    all_user_ids = user_ids.union(related_user_ids)

    # 5. Fetch all Users in one query
    users = db.query(User).filter(User.id.in_(all_user_ids)).all()
    user_map = {u.id: u for u in users}

    # 6. Build Responses in Memory
    response_list = []

    for pix in transactions:
        # Default values
        sender_name = "Unknown"
        sender_doc = "***"
        receiver_name = "Unknown"
        receiver_doc = "***"

        owner_user = user_map.get(pix.user_id)

        if pix.type == TransactionType.SENT:
            # Owner is Sender
            if owner_user:
                sender_name = owner_user.name
                sender_doc = mask_cpf_cnpj(owner_user.cpf_cnpj)

            # Find Receiver (Counterpart)
            receiver_tx = related_tx_map.get(pix.correlation_id)
            # Ensure it's the right type (RECEIVED)
            if receiver_tx and receiver_tx.type == TransactionType.RECEIVED:
                receiver_user = user_map.get(receiver_tx.user_id)
                if receiver_user:
                    receiver_name = receiver_user.name
                    receiver_doc = mask_cpf_cnpj(receiver_user.cpf_cnpj)
            else:
                # External — use stored recipient name when available
                receiver_name = pix.recipient_name or "Destinatario externo"
                receiver_doc = mask_cpf_cnpj(pix.pix_key)

        elif pix.type == TransactionType.RECEIVED:
            # Owner is Receiver
            if owner_user:
                receiver_name = owner_user.name
                receiver_doc = mask_cpf_cnpj(owner_user.cpf_cnpj)

            # Find Sender (Counterpart)
            sender_tx = related_tx_map.get(pix.correlation_id)
            # Ensure it's the right type (SENT)
            if sender_tx and sender_tx.type == TransactionType.SENT:
                sender_user = user_map.get(sender_tx.user_id)
                if sender_user:
                    sender_name = sender_user.name
                    sender_doc = mask_cpf_cnpj(sender_user.cpf_cnpj)
            else:
                # Deposito ou externo
                if "SIMULACAO" in pix.pix_key or "Deposit" in (pix.description or ""):
                    sender_name = "Deposito via QR Code"
                    sender_doc = "Instituicao Financeira"
                else:
                    sender_name = pix.recipient_name or "Pagador externo"
                    sender_doc = "***"

        response_list.append(PixResponse(
            id=pix.id,
            value=pix.value,
            pix_key=pix.pix_key,
            key_type=pix.key_type,
            type=pix.type,
            status=pix.status,
            description=pix.description,
            scheduled_date=pix.scheduled_date,
            created_at=pix.created_at,
            updated_at=pix.updated_at,
            formatted_time=format_brasilia_time(pix.created_at),
            sender_name=sender_name,
            sender_doc=sender_doc,
            receiver_name=receiver_name,
            receiver_doc=receiver_doc
        ))

    return PixStatementResponse(
        total_transactions=result["total_transactions"],
        total_value=result["total_value"],
        balance=result["balance"],
        transactions=response_list
    )


def _normalize_pix_key(chave: str, tipo: str) -> str:
    """
    Normalizes a PIX key to the format expected by Asaas before sending to the API.

    Rules:
    - TELEFONE: strip all non-digits, ensure E.164 format (+55DDDNNNNNNNNN)
    - CPF: strip all non-digits (11 digits)
    - CNPJ: strip all non-digits (14 digits)
    - EMAIL: lowercase and strip whitespace
    - ALEATORIA / EVP: strip whitespace only
    """
    import re as _re

    if tipo in ("TELEFONE", "PHONE"):
        digits = _re.sub(r"\D", "", chave)
        # Remove leading country code if already present (55...)
        if digits.startswith("55") and len(digits) > 11:
            digits = digits[2:]
        # digits should now be DDD + number (10 or 11 digits)
        return f"+55{digits}"

    if tipo in ("CPF", "CNPJ"):
        return _re.sub(r"\D", "", chave)

    if tipo == "EMAIL":
        return chave.strip().lower()

    # ALEATORIA / EVP / unknown — trim only
    return chave.strip()


@router.get("/consultar-chave", response_model=Dict[str, Any])
def lookup_pix_key_endpoint(
    chave: str,
    tipo: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Validates a PIX key and returns beneficiary (recipient) information.
    Priority: internal BioCodeTechPay users -> Asaas gateway -> key format valid.
    The key is normalized before any lookup to ensure correct format for Asaas.
    """
    import re as _re

    # 1. Resolve key type enum — accept both enum value ("TELEFONE") and name ("PHONE")
    from app.pix.schemas import PixKeyType as _PKT
    try:
        key_type_enum = _PKT(tipo)
    except ValueError:
        try:
            key_type_enum = _PKT[tipo]  # e.g. "PHONE" -> PixKeyType.PHONE
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Tipo de chave inválido: '{tipo}'. Valores aceitos: CPF, CNPJ, EMAIL, TELEFONE, ALEATORIA")

    # 2. Normalize the raw key to its canonical form
    chave_normalizada = _normalize_pix_key(chave.strip(), tipo)

    # 3. Check internal BioCodeTechPay users first (use original for email, normalized for cpf/phone)
    recipient = find_recipient_user(db, chave_normalizada, key_type_enum)
    if not recipient and chave_normalizada != chave.strip():
        # Fallback: try with raw value in case internal store uses different format
        recipient = find_recipient_user(db, chave.strip(), key_type_enum)

    if recipient:
        return {
            "found": True,
            "name": recipient.name,
            "document": mask_cpf_cnpj(recipient.cpf_cnpj),
            "bank": "BioCodeTechPay",
            "internal": True,
        }

    # 4. Try gateway lookup with normalized key
    gateway = get_payment_gateway()
    if gateway:
        try:
            info = gateway.lookup_pix_key(chave_normalizada, tipo)

            if info is None:
                # Gateway indisponivel (rede, sandbox, erro 5xx) — soft pass: nao bloquear envio
                return {
                    "found": True,
                    "name": "Destinatário não identificado",
                    "document": "***",
                    "bank": "Transferência via rede PIX",
                    "internal": False,
                    "unverified": True,
                }

            if info.get("found") is False:
                reason = info.get("reason", "not_in_dict")
                if reason == "invalid_format":
                    return {
                        "found": False,
                        "error": "Formato de chave inválido para o tipo selecionado.",
                    }
                # 404 do DICT: chave nao cadastrada no Asaas sandbox, mas pode existir em outro banco.
                # Soft pass — nao bloquear; a rede PIX valida no momento do envio.
                return {
                    "found": True,
                    "name": "Destinatário não identificado",
                    "document": "***",
                    "bank": "Transferência via rede PIX",
                    "internal": False,
                    "unverified": True,
                }

            if info.get("name"):
                return {
                    "found": True,
                    "name": info["name"],
                    "document": info.get("document", "***"),
                    "bank": info.get("bank", "Rede Bancária"),
                    "internal": False,
                }

        except Exception:
            pass  # Gateway completamente indisponivel — soft pass abaixo

    # 5. Sem gateway configurado ou erro inesperado — soft pass para nao bloquear envio
    return {
        "found": True,
        "name": "Destinatário não identificado",
        "document": "***",
        "bank": "Transferência via rede PIX",
        "internal": False,
        "unverified": True,
    }


@router.post("/cobrar", response_model=PixChargeResponse)
def generate_pix_charge(
    data: PixChargeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixChargeResponse:
    """
    Generates a PIX Charge (Receive Money).
    Attempts real Asaas charge first; falls back to local simulation.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(f"Generating PIX charge: value={data.value} for user {current_user.id}")

    description = data.description or "BioCodeTechPay - Cobranca PIX"

    ASAAS_MIN_VALUE = Decimal("0.01")

    # --- Attempt real Asaas charge (gateway must be configured; any value >= R$0.01) ---
    gateway = get_payment_gateway()
    if gateway and Decimal(str(data.value)) >= ASAAS_MIN_VALUE:
        try:
            customer_id = ensure_asaas_customer(db, current_user.id)
            if customer_id:
                charge_data = gateway.create_pix_charge(
                    value=Decimal(str(data.value)),
                    description=description,
                    customer_id=customer_id,
                    due_date=_dt.combine(data.due_date, _dt.min.time()) if data.due_date else None,
                    idempotency_key=f"cobrar-{correlation_id}"
                )

                # Store transaction with Asaas payment ID
                pix = PixTransaction(
                    id=charge_data["charge_id"],
                    value=data.value,
                    pix_key=charge_data.get("qr_code", "")[:200],  # VARCHAR(200) — truncate for pix_key lookup
                    key_type=PixKeyType.RANDOM.value,
                    type=TransactionType.RECEIVED,
                    status=PixStatus.CREATED,
                    idempotency_key=f"cobrar-{correlation_id}",
                    description=description,
                    correlation_id=correlation_id,
                    user_id=current_user.id,
                    copy_paste_code=charge_data.get("qr_code", ""),  # full EMV, no truncation
                    expires_at=charge_data.get("expires_at")
                )
                db.add(pix)
                db.commit()
                db.refresh(pix)

                # Asaas returns base64 image — prefix for data URI
                raw_image = charge_data.get("qr_code_url", "")
                if raw_image and not raw_image.startswith("data:"):
                    qr_url = f"data:image/png;base64,{raw_image}"
                else:
                    qr_url = raw_image

                logger.info(f"Real Asaas charge created: {pix.id}")
                return PixChargeResponse(
                    charge_id=pix.id,
                    value=data.value,
                    description=description,
                    copy_and_paste=charge_data.get("qr_code", ""),
                    qr_code_url=qr_url,
                    is_real_charge=True,
                    expires_at=pix.expires_at
                )
        except Exception as e:
            logger.warning(f"Asaas charge failed, falling back to simulation: {str(e)}")
            db.rollback()  # reset session state before fallback insert
    elif gateway and Decimal(str(data.value)) < ASAAS_MIN_VALUE:
        logger.info(
            f"Value R${data.value:.2f} is below Asaas minimum R$0.01 — using local simulation."
        )

    # --- Fallback: local simulation with valid BR Code EMV ---
    # Generates a format-valid, CRC-valid PIX EMV payload so any bank app
    # can parse and display the charge (key lookup at DICT will not resolve in
    # sandbox — that is expected; in production all charges go through Asaas).
    logger.info(f"Creating local simulation charge for user {current_user.id}")
    charge_id = str(uuid4())

    # Build EMV before insert so it can be persisted in copy_paste_code
    emv_payload = _build_pix_static_emv(charge_id, data.value)

    pix = PixTransaction(
        id=charge_id,
        value=data.value,
        pix_key=charge_id,               # UUID stored: route 1a in _find_internal_qrcode_charge finds it
        key_type=PixKeyType.RANDOM.value,
        type=TransactionType.RECEIVED,
        status=PixStatus.CREATED,
        idempotency_key=f"charge-{charge_id}",
        description=description,
        correlation_id=correlation_id,
        user_id=current_user.id,
        copy_paste_code=emv_payload,      # stored for shareable payment link
        expires_at=_dt.combine(data.due_date, _dt.min.time()) if data.due_date else (_dt.now() + _td(hours=24))
    )

    db.add(pix)
    db.commit()
    db.refresh(pix)

    # QR code image encodes the EMV payload directly — any BR Code reader (any bank app,
    # any POS terminal) can scan this and extract the PIX data correctly.
    qr_url = _build_qr_url(emv_payload)

    logger.info(
        f"Simulation charge created: id={charge_id}, value={data.value:.2f}, "
        f"emv_len={len(emv_payload)}, crc={emv_payload[-4:]}"
    )

    return PixChargeResponse(
        charge_id=charge_id,
        value=data.value,
        description=description,
        copy_and_paste=emv_payload,
        qr_code_url=qr_url,
        is_real_charge=False,
        expires_at=pix.expires_at
    )


@router.post("/receber/confirmar", response_model=PixResponse)
def process_pix_receipt(
    data: PixChargeConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Processes a received PIX (Deposit) for a specific Charge ID.
    Enforces One-Time Use: If charge is already paid, rejects.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(f"Processing PIX receipt for charge: {data.charge_id}")

    # Find the charge transaction
    pix = db.query(PixTransaction).filter(PixTransaction.id == data.charge_id).first()

    if not pix:
        logger.error(f"Charge not found: {data.charge_id}")
        raise HTTPException(status_code=404, detail="Cobrança não encontrada.")

    logger.info(f"Charge found: {pix.id}, Status: {pix.status}, Value: {pix.value}")

    # CRITICAL: One-Time Use Check
    if pix.status == PixStatus.CONFIRMED:
        logger.warning(f"Attempt to reuse paid charge: {data.charge_id}")
        raise HTTPException(status_code=409, detail="Esta cobrança já foi paga e não pode ser utilizada novamente.")

    if pix.status != PixStatus.CREATED:
        logger.error(f"Invalid charge status: {pix.status} for charge {data.charge_id}")
        raise HTTPException(status_code=400, detail=f"Status da cobrança inválido: {pix.status}")

    try:
        # Confirm the transaction
        pix.status = PixStatus.CONFIRMED
        db.add(pix)

        # Credit the receiver balance (User who created the charge)
        # Deposits are free (fee=R$0.00). Full gross value credited.
        receiver_user = db.query(User).filter(User.id == pix.user_id).first()
        if receiver_user:
            credit_pix_receipt(
                db, receiver_user, float(pix.value),
                source=f"receber_confirmar:charge_id={pix.id}",
            )
        else:
            logger.warning(f"Receiver user not found for charge {pix.id} (User ID: {pix.user_id})")

        db.commit()
        db.refresh(pix)

        logger.info(f"Charge {pix.id} successfully confirmed.")
        return build_pix_response(pix, db)

    except Exception as e:
        db.rollback()
        logger.error(f"Error processing receipt: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error processing deposit")


@router.post("/cobrar/{charge_id}/verificar", response_model=PixResponse)
def verify_pix_charge_payment(
    charge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> PixResponse:
    """
    Verifies if a real Asaas PIX charge has been paid.
    When confirmed by Asaas, credits user.balance and marks transaction CONFIRMED.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    pix = db.query(PixTransaction).filter(
        PixTransaction.id == charge_id,
        PixTransaction.user_id == current_user.id
    ).first()

    if not pix:
        raise HTTPException(status_code=404, detail="Cobrança não encontrada.")

    if pix.status == PixStatus.CONFIRMED:
        return build_pix_response(pix, db)

    if pix.status != PixStatus.CREATED:
        raise HTTPException(status_code=400, detail=f"Status inválido: {pix.status}")

    gateway = get_payment_gateway()
    if not gateway:
        raise HTTPException(status_code=503, detail="Serviço de pagamento temporariamente indisponível.")

    try:
        charge_status = gateway.get_charge_status(charge_id)
        logger.info(f"Asaas charge status: {charge_id} -> {charge_status.get('status')}")

        if charge_status.get("status") == "CONFIRMED":
            pix.status = PixStatus.CONFIRMED
            db.add(pix)

            receiver_user = db.query(User).filter(User.id == pix.user_id).first()
            if receiver_user:
                credit_pix_receipt(
                    db, receiver_user, float(pix.value),
                    source=f"verificar_charge:charge_id={charge_id}",
                )

            db.commit()
            db.refresh(pix)
            return build_pix_response(pix, db)

        # Not paid yet
        raise HTTPException(
            status_code=202,
            detail=f"Pagamento ainda não confirmado. Status: {charge_status.get('status', 'PENDING')}. Aguarde e tente novamente."
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error verifying Asaas charge {charge_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao verificar o pagamento. Tente novamente.")


@router.post("/deposito/verificar", status_code=200)
def verify_static_deposit(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None),
):
    """
    Active verification for PIX deposits made to the platform static QR code.

    The static QR (shared EVP key) relies on webhooks to credit balances.
    This endpoint provides a fallback: queries Asaas ``GET /pix/transactions``
    for recent PIX CREDITs, matches sender CPF/CNPJ to the authenticated user,
    and credits any deposits not yet processed by the webhook.

    Idempotent: safe to call repeatedly without double-credit risk.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    user_doc_raw = re.sub(r"\D", "", current_user.cpf_cnpj or "")
    if not user_doc_raw:
        raise HTTPException(status_code=400, detail="CPF/CNPJ nao cadastrado.")

    gateway = get_payment_gateway()
    if not gateway:
        raise HTTPException(
            status_code=503,
            detail="Servico de pagamento temporariamente indisponivel.",
        )

    now = _dt.now()
    start = (now - _td(hours=48)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    credits = gateway.list_pix_credits(start, end)

    credited_count = 0
    credited_total = Decimal("0.00")

    for tx in credits:
        ext = tx.get("externalAccount") or {}
        sender_doc = re.sub(r"\D", "", ext.get("cpfCnpj") or "")

        # -- CPF/CNPJ match against current user --------------------------
        matches = False
        if sender_doc and user_doc_raw:
            if sender_doc == user_doc_raw:
                matches = True
            elif len(sender_doc) == 6 and len(user_doc_raw) == 11:
                # Asaas-masked CPF: ***.XXX.XXX-** -> 6 visible middle digits
                matches = user_doc_raw[3:9] == sender_doc
            elif len(sender_doc) >= 6:
                matches = sender_doc in user_doc_raw

        if not matches:
            continue

        tx_id = tx.get("id") or str(uuid4())
        value = float(tx.get("value") or 0)
        if value <= 0:
            continue

        # -- Idempotency: skip if already processed -----------------------
        idemp_key = f"deposit-verify-{tx_id}"
        already = db.query(PixTransaction.id).filter(
            PixTransaction.idempotency_key == idemp_key
        ).first()
        if already:
            continue

        # Also skip if the webhook already credited via payment_id
        payment_id = tx.get("payment")
        if payment_id:
            webhook_tx = db.query(PixTransaction.id).filter(
                PixTransaction.id == payment_id
            ).first()
            if webhook_tx:
                continue

        # -- Credit balance ------------------------------------------------
        payer_name = (ext.get("name") or "Deposito PIX").strip()
        net_credit, fee_float = credit_pix_receipt(
            db, current_user, value,
            source=f"deposit_verify:pix_tx={tx_id}",
        )

        new_tx = PixTransaction(
            id=tx_id,
            value=net_credit,
            pix_key=_PLATFORM_PIX_KEY,
            key_type="ALEATORIA",
            type=TransactionType.RECEIVED,
            status=PixStatus.CONFIRMED,
            user_id=current_user.id,
            idempotency_key=idemp_key,
            description=f"PIX recebido de {payer_name}",
            recipient_name=payer_name,
            fee_amount=fee_float,
        )
        db.add(new_tx)

        credited_count += 1
        credited_total += Decimal(str(net_credit))

        logger.info(
            f"Deposit verified: user={current_user.id} tx={tx_id} "
            f"gross=R${value:.2f} fee=R${fee_float:.2f} net=R${net_credit:.2f}"
        )
        audit_log(
            action="PIX_DEPOSIT_VERIFIED",
            user=current_user.id,
            resource=f"pix_tx={tx_id}",
            details={
                "payer_name": payer_name,
                "gross_value": value,
                "fee": fee_float,
                "net_credit": float(net_credit),
            },
        )

    if credited_count > 0:
        db.commit()
        db.refresh(current_user)

    return {
        "credited_count": credited_count,
        "credited_total": float(credited_total),
        "balance": float(current_user.balance),
    }


@router.post("/qrcode/consultar", response_model=Dict[str, Any], status_code=200)
def consultar_pix_qrcode(
    data: PixQrCodeConsultarRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> Dict[str, Any]:
    """
    Resolves value and beneficiary of a PIX QR Code payload before payment.
    Value is always locked server-side — never derived from client input.
    Call this before /qrcode/pagar to guarantee the correct payment amount.
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    # -------------------------------------------------------------------------
    # CRC validation (BACEN spec, field 63, CRC-16/CCITT-FALSE)
    # Reject payloads with a corrupt checksum before any database or network call.
    # A valid bank QR always passes. Internal/test QR without field 63 also pass.
    # -------------------------------------------------------------------------
    if not _validate_pix_crc(data.payload):
        logger.warning("QR consultar: CRC validation failed — payload may be tampered")
        raise HTTPException(
            status_code=422,
            detail="QR Code invalido: checksum incorreto. O codigo pode estar danificado ou alterado."
        )

    internal_charge, is_already_paid = _find_internal_qrcode_charge(data.payload, db, logger)

    if is_already_paid:
        raise HTTPException(status_code=409, detail="Esta cobrança já foi paga.")

    if internal_charge:
        receiver = db.query(User).filter(User.id == internal_charge.user_id).first()
        beneficiary_name = receiver.name if receiver else "Correntista BioCodeTechPay"
        return {
            "value": float(internal_charge.value),
            "beneficiary_name": beneficiary_name,
            "is_internal": True,
            "charge_id": internal_charge.id
        }

    # -------------------------------------------------------------------------
    # Three-stage decode chain for external QR Codes
    # -------------------------------------------------------------------------
    #
    # Correct priority (BR Code / BACEN Manual v2.1):
    #
    # Stage 1 — payloadLocation URL (field 26, sub-tag 25 OR 01)
    #   MUST run first when URL is present. Dynamic QR codes from maquininhas
    #   (PagSeguro, Mercado Pago, Stone, Cielo, Rede) can have field-54 WITH a
    #   value AND a payloadLocation URL. Field-54 carries the amount but the
    #   CHARGE may already be EXPIRADA at the PSP. Only the URL endpoint is the
    #   authoritative liveness check. Fetching the URL is PSP-direct (no Asaas)
    #   so it works in both sandbox and production.
    #
    # Stage 2 — Local EMV field-54 parse (zero latency, no network)
    #   Used ONLY when no URL is detected OR when Stage 1 fails with a network
    #   error (not an expiry). Static QR codes (most bank transfers) land here.
    #   Field-54 parse never raises — worst case it returns 0.0.
    #
    # Stage 3 — Asaas /pix/qrCodes/decode (fallback of last resort)
    #   Covers edge cases not handled by the above two stages. Swallows all
    #   errors because sandbox mode makes this fail for real-world QR codes.

    # Stage 1: resolve payloadLocation URL directly at PSP.
    # Only entered when the QR has a genuine URL in fields 26-51 sub-tag 25/01.
    # Static QRs (CNPJ key, CPF key, etc.) have no URL and skip this stage.
    pix_url = _extract_pix_url(data.payload)
    if pix_url:
        logger.info(f"QR consultar: stage-1 payloadLocation found url={pix_url[:80]}")
        try:
            charge_data = _fetch_pix_charge_url(pix_url)
            # txid anti-fraud validation — runs inside try where charge_data is in scope.
            txid_qr = _extract_txid_field62(data.payload)
            txid_psp = charge_data.get("txid")
            if txid_qr and txid_psp and txid_qr.upper() != txid_psp.upper():
                logger.warning(
                    f"QR consultar: txid mismatch txid_qr={txid_qr} txid_psp={txid_psp}"
                )
                raise HTTPException(
                    status_code=422,
                    detail="QR Code invalido: identificador da cobranca diverge do registrado no PSP."
                )
            logger.info(f"QR consultar: stage-1 PSP resolve success value={charge_data['value']}")
            return {
                "value": charge_data["value"],
                "beneficiary_name": charge_data.get("beneficiary_name") or "Beneficiario",
                "is_internal": False,
                "charge_id": None
            }
        except HTTPException:
            raise  # txid mismatch — must not be swallowed by the generic handler below
        except _PixChargeExpired as exp_status:
            # PSP confirmed terminal status (EXPIRADA/CONCLUIDA or HTTP 404/410).
            # Do NOT fall through: Asaas will also reject this QR at payment time.
            logger.warning(f"QR consultar: stage-1 charge terminal status={exp_status}")
            raise HTTPException(
                status_code=422,
                detail=(
                    "QR Code expirado ou ja utilizado. "
                    "Gere um novo QR Code no terminal e tente novamente."
                )
            )
        except Exception as url_err:
            # PSP unreachable, auth required, or rate-limited — fall through to field-54.
            logger.warning(f"QR consultar: stage-1 PSP unreachable, fallback to field-54: {url_err}")

    # Stage 2: local EMV field-54 parse — no network, zero latency.
    # Handles static QRs (no payloadLocation URL) and fallback when Stage 1 PSP
    # was temporarily unreachable. Static QRs do not expire by design.
    emv_value = _parse_emv_value(data.payload)
    if emv_value > 0:
        merchant_name = _parse_emv_top_level(data.payload.strip()).get("59", "").strip()
        logger.info(f"QR consultar: stage-2 field-54 value={emv_value} merchant={merchant_name!r}")
        return {
            "value": emv_value,
            "beneficiary_name": merchant_name or "Beneficiario",
            "is_internal": False,
            "charge_id": None
        }

    # Stage 3: Asaas /pix/qrCodes/decode — fallback of last resort.
    # In sandbox mode Asaas rejects external QR codes — errors are not surfaced.
    # In production, Asaas may resolve value/liveness for additional PSPs.
    gateway = get_payment_gateway()
    if gateway:
        try:
            decoded = gateway.decode_qr_code(data.payload)
            if decoded and decoded.get("value"):
                logger.info(f"QR consultar: stage-3 Asaas decode success value={decoded['value']}")
                return {
                    "value": float(decoded["value"]),
                    "beneficiary_name": decoded.get("beneficiary_name") or "Beneficiario",
                    "is_internal": False,
                    "charge_id": None
                }
        except httpx.HTTPStatusError as asaas_http_err:
            logger.warning(f"QR consultar: stage-3 Asaas decode HTTP error: {asaas_http_err}")
        except Exception as e:
            logger.warning(f"QR consultar: stage-3 Asaas decode failed: {e}")

    raise HTTPException(
        status_code=422,
        detail=(
            "Nao foi possivel determinar o valor deste QR Code. "
            "Se for QR dinamico de maquininha, gere um novo QR e tente novamente."
        )
    )


@router.post("/qrcode/pagar", response_model=Dict[str, Any], status_code=200)
def pay_pix_qrcode(
    data: PixQrCodePayRequest,
    x_idempotency_key: str = Header(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> Dict[str, Any]:
    """
    Pays a PIX QR Code (scanned or Pix Copia e Cola EMV payload).

    Routing logic:
    1. If the EMV payload contains an internal BioCodeTechPay charge UUID -> confirm locally.
    2. Otherwise -> dispatch to Asaas POST /pix/qrCodes/pay.

    - **payload**: Full EMV string (000201...) or Pix Copia e Cola code
    - **description**: Optional description (max 140 chars)
    """
    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    logger.info(
        f"QR Code payment request: user={current_user.id}, "
        f"payload_length={len(data.payload)}, idempotency={x_idempotency_key}"
    )

    # CRC validation (same check as /consultar — reject corrupted payloads early)
    if not _validate_pix_crc(data.payload):
        logger.warning("QR pagar: CRC validation failed — payload may be tampered")
        raise HTTPException(
            status_code=422,
            detail="QR Code invalido: checksum incorreto. O codigo pode estar danificado ou alterado."
        )

    idempotency_key = x_idempotency_key or str(uuid4())

    # Idempotency guard: reject duplicate payment attempts
    if x_idempotency_key:
        existing = db.query(PixTransaction).filter(
            PixTransaction.idempotency_key == x_idempotency_key
        ).first()
        if existing:
            logger.info(f"Duplicate QR Code payment blocked: idempotency_key={x_idempotency_key}")
            return build_pix_response(existing, db).model_dump()

    # Payload-hash guard: server-side deduplication by EMV content.
    # Blocks retries regardless of what idempotency header the frontend sends.
    # Normalization: strip whitespace only — EMV field values are case-sensitive per BACEN spec.
    import hashlib as _hl
    _norm_payload = data.payload.strip()
    _payload_hash = _hl.sha256(_norm_payload.encode()).hexdigest()
    _existing_by_hash = db.query(PixTransaction).filter(
        PixTransaction.user_id == current_user.id,
        PixTransaction.payload_hash == _payload_hash,
        PixTransaction.status.in_([PixStatus.CONFIRMED, PixStatus.PROCESSING])
    ).first()
    if _existing_by_hash:
        logger.info(
            f"Duplicate QR payment blocked by payload_hash: {_payload_hash[:16]}... "
            f"existing_tx={_existing_by_hash.id}"
        )
        return build_pix_response(_existing_by_hash, db).model_dump()

    sender = db.query(User).filter(User.id == current_user.id).first()
    if not sender:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    # Anti-fraud screening on outbound QR payment
    _screen_value = getattr(data, "value", None) or 0.0
    if _screen_value > 0:
        _screen_antifraud(_screen_value, current_user.id, db, logger, tx_type="PIX_QR_PAY")

    # Resolve internal/external routing — shared helper used by /consultar and /pagar
    internal_charge, is_already_paid = _find_internal_qrcode_charge(data.payload, db, logger)
    if is_already_paid:
        raise HTTPException(status_code=409, detail="Esta cobrança já foi paga.")

    if internal_charge:
        charge_value = float(internal_charge.value)
        receiver = db.query(User).filter(User.id == internal_charge.user_id).first()
        if not receiver:
            raise HTTPException(status_code=422, detail="Recebedor da cobrança não encontrado.")

        logger.info(
            f"Internal charge detected: charge_id={internal_charge.id}, "
            f"payer={current_user.id}, receiver={receiver.id}, value={charge_value}"
        )

        if internal_charge.status != PixStatus.CREATED:
            raise HTTPException(status_code=409, detail="Esta cobrança já foi paga.")

        is_self_deposit = (internal_charge.user_id == current_user.id)

        if not is_self_deposit:
            from app.core.fees import PIX_MAINTENANCE_FEE as _qr_maint
            from app.core.matrix import credit_fee as _qr_credit_internal
            _maint_dec = _qr_maint  # already Decimal
            _charge_dec = Decimal(str(charge_value))
            _total_debit = _charge_dec + _maint_dec
            if sender.balance < _total_debit:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Saldo insuficiente. Disponível: R$ {sender.balance:.2f}, "
                        f"Necessário: R$ {_total_debit:.2f} "
                        f"(valor R$ {charge_value:.2f} + taxa de manutenção R$ {_maint_dec:.2f})"
                    )
                )
            previous_balance = sender.balance
            sender.balance = Decimal(str(sender.balance)) - _total_debit
            if sender.balance < 0:
                db.rollback()
                logger.error(
                    f"BALANCE_INVARIANT_VIOLATION [internal_qr]: user={sender.id} "
                    f"post-debit={sender.balance:.2f} total_debit={_total_debit:.2f}. Rolled back."
                )
                raise HTTPException(
                    status_code=400,
                    detail="Saldo insuficiente. Operação cancelada por proteção de saldo."
                )
            db.add(sender)
            _qr_credit_internal(db, float(_maint_dec))
            logger.info(
                f"Internal QR payment: debited payer={sender.id}, "
                f"amount=R${charge_value:.2f}, fee=R${_maint_dec:.2f}, "
                f"balance: R${previous_balance:.2f} -> R${sender.balance:.2f}"
            )

        internal_charge.status = PixStatus.CONFIRMED
        db.add(internal_charge)
        credit_pix_receipt(
            db, receiver, float(charge_value),
            source=f"internal_qr_payment:charge_id={internal_charge.id}",
            fee_override=0.0,
        )

        if not is_self_deposit:
            sent_pix = PixTransaction(
                id=str(uuid4()),
                value=float(charge_value),
                pix_key=internal_charge.pix_key,
                key_type=PixKeyType.RANDOM.value,
                type=TransactionType.SENT,
                status=PixStatus.CONFIRMED,
                idempotency_key=idempotency_key,
                description=data.description or "PIX QR Code Payment",
                correlation_id=internal_charge.correlation_id,
                user_id=current_user.id,
                recipient_name=receiver.name,
                payload_hash=_payload_hash
            )
            db.add(sent_pix)
            db.commit()
            db.refresh(sent_pix)
            audit_log(
                action="PIX_QRCODE_INTERNAL_PAYMENT",
                user=str(current_user.id),
                resource=f"charge_id={internal_charge.id}",
                details={
                    "charge_id": internal_charge.id,
                    "value": float(charge_value),
                    "maintenance_fee": float(_maint_dec),
                    "total_debit": float(_total_debit),
                    "receiver_id": str(receiver.id)
                }
            )
            result_dict = build_pix_response(sent_pix, db).model_dump()
            result_dict["receiver_name"] = receiver.name
            return result_dict
        else:
            db.commit()
            db.refresh(internal_charge)
            return build_pix_response(internal_charge, db).model_dump()

    # -------------------------------------------------------------------------
    # Routing 2: no internal charge found — dispatch to Asaas.
    # -------------------------------------------------------------------------
    # Pre-flight balance check: use EMV field-54 value when available so the user
    # receives a meaningful error BEFORE the Asaas API call is dispatched.
    # Available balance considers pending outbound (PROCESSING) transactions.
    from app.pix.service import get_available_balance as _pre_avail
    _pre_available = _pre_avail(db, current_user.id)
    _pre_emv_val = _parse_emv_value(data.payload)
    if _pre_emv_val > 0:
        from app.core.fees import calculate_pix_fee as _pre_fee_calc, fee_display as _pre_fee_display
        _pre_fee = float(_pre_fee_calc(sender.cpf_cnpj, _pre_emv_val, is_external=True, is_received=False))
        _pre_total = _pre_emv_val + _pre_fee
        if _pre_available < _pre_total:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Saldo insuficiente. Disponivel: R$ {_pre_available:.2f}, "
                    f"Necessario: R$ {_pre_total:.2f} "
                    f"(valor R$ {_pre_emv_val:.2f} + taxa {_pre_fee_display(_pre_fee)})"
                )
            )
    elif _pre_available <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Saldo insuficiente. Disponivel: R$ {_pre_available:.2f}"
        )

    gateway = get_payment_gateway()
    if not gateway:
        raise HTTPException(
            status_code=503,
            detail="Servico de pagamento temporariamente indisponivel."
        )

    # Sandbox guard: Asaas sandbox cannot communicate with the real SPI/DICT.
    # Any payment attempt against a real-world maquininha QR will be rejected.
    # This surfaces the configuration issue immediately with a clear message.
    from app.core.config import settings as _settings
    if _settings.ASAAS_USE_SANDBOX:
        raise HTTPException(
            status_code=422,
            detail=(
                "Pagamento de QR Code externo requer modo producao. "
                "O sistema esta configurado em modo sandbox (ASAAS_USE_SANDBOX=True). "
                "Defina ASAAS_USE_SANDBOX=False no Render Dashboard e use a chave de producao Asaas."
            )
        )

    # ── Persist-before-dispatch: create transaction BEFORE calling Asaas ──
    # This prevents orphaned PSP transactions if the app crashes between
    # dispatch and persist. The transaction starts as CREATED, moves to
    # PROCESSING after successful dispatch, or FAILED on error.
    _provisional_id = str(uuid4())
    from app.pix.service import create_ledger_entry as _qr_ledger
    from app.pix.models import LedgerEntryType as _QR_LET

    # Pre-resolve EMV value for fee calculation before dispatch
    _pre_dispatch_emv_val = _parse_emv_value(data.payload)
    _pre_dispatch_fee = float(calculate_pix_fee(
        sender.cpf_cnpj,
        _pre_dispatch_emv_val if _pre_dispatch_emv_val > 0 else (data.value or 0.01),
        is_external=True,
        is_received=False,
    )) if _pre_dispatch_emv_val > 0 or data.value else 0.0
    _pre_dispatch_pix_key_ref = data.payload[:197] + "..." if len(data.payload) > 200 else data.payload

    pix = PixTransaction(
        id=_provisional_id,
        value=_pre_dispatch_emv_val if _pre_dispatch_emv_val > 0 else (data.value or 0.01),
        fee_amount=_pre_dispatch_fee,
        pix_key=_pre_dispatch_pix_key_ref,
        key_type=PixKeyType.RANDOM.value,
        type=TransactionType.SENT,
        status=PixStatus.CREATED,
        idempotency_key=idempotency_key,
        description=data.description or "PIX QR Code Payment",
        correlation_id=correlation_id,
        user_id=current_user.id,
        payload_hash=_payload_hash,
    )
    db.add(pix)
    db.flush()  # persist row but don't commit — allows rollback if dispatch fails catastrophically

    logger.info(
        f"QR Code payment persisted (pre-dispatch): id={_provisional_id}, "
        f"user={sender.id}, status=CREATED, emv_value={_pre_dispatch_emv_val}, payload_hash={_payload_hash}"
    )

    try:
        result = gateway.pay_qr_code(
            payload=data.payload,
            description=data.description or "BioCodeTechPay QR Code Payment",
            idempotency_key=idempotency_key,
            value=data.value,
        )
    except Exception as e:
        # Dispatch failed: mark transaction as FAILED and persist
        pix.status = PixStatus.FAILED
        db.commit()

        error_msg = str(e)
        logger.error(f"Asaas QR Code payment failed: {error_msg}", exc_info=True)
        try:
            import json as _json
            detail_raw = getattr(e, 'response', None)
            if detail_raw is not None:
                body = _json.loads(detail_raw.text)
                errors = body.get("errors", [])
                if errors:
                    error_msg = "; ".join(
                        err.get("description") or err.get("code", "erro desconhecido")
                        for err in errors
                    )
        except Exception:
            pass
        # Preserve explicit error messages raised by the adapter (e.g. expired dynamic QR,
        # missing PSP key). These already carry user-facing Portuguese text and must not
        # be replaced by the generic "expirado" fallback.
        adapter_explicit = any(
            marker in error_msg
            for marker in (
                "expirado",
                "Gere um novo QR Code",
                "PSP nao retornou",
                "nao suportado",
                "Valor do QR Code",
                "JWS invalido",
                "formato JWS",
                "resposta vazia",
            )
        )
        # Translate raw Asaas API errors to actionable Portuguese messages
        if not adapter_explicit and (
            "qrCode' informado" in error_msg
            or "qrCode informado" in error_msg
            or "invalid" in error_msg.lower()
        ):
            error_msg = (
                "QR Code invalido ou expirado. "
                "QR Codes dinamicos de maquininhas expiram em 60 a 300 segundos. "
                "Gere um novo QR Code no terminal e tente novamente."
            )
        raise HTTPException(status_code=422, detail=error_msg)

    asaas_value = float(result.get("value") or 0)
    emv_value = _parse_emv_value(data.payload)

    # Value MUST come from Asaas response or EMV field 54 — client input is never trusted.
    payment_value = asaas_value or emv_value

    logger.info(
        f"QR payment value resolution: asaas={asaas_value}, emv={emv_value}, "
        f"resolved={payment_value}"
    )

    # SECURITY: client-provided value (data.value) is NEVER trusted as payment amount.
    # Only Asaas response value or EMV field-54 are authoritative sources.
    # If neither provides a value after gateway dispatch, manual reconciliation is required.
    if payment_value <= 0:
        logger.critical(
            f"QR payment dispatched but value unresolvable: "
            f"asaas_value={asaas_value}, emv_value={emv_value}, "
            f"client_value_rejected={data.value}, payment_id={result.get('payment_id')}"
        )
        audit_log(
            action="PIX_QRCODE_VALUE_UNRESOLVABLE",
            user=str(current_user.id),
            resource=f"payment_id={result.get('payment_id')}",
            details={
                "asaas_value": asaas_value,
                "emv_value": emv_value,
                "client_value_rejected": data.value,
                "gateway_status": result.get("status"),
            }
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Pagamento foi processado pelo gateway mas o valor nao pode ser determinado. "
                "Entre em contato com o suporte para reconciliacao manual. "
                f"Referencia: {result.get('payment_id', 'N/A')}"
            )
        )

    # Calculate platform fee on the confirmed payment value.
    # Mirrors the logic in service.py for regular PIX key transfers.
    from app.core.fees import calculate_pix_fee as _qr_fee_calc, PLATFORM_PIX_OUTBOUND_NETWORK_FEE as _QR_NET_FEE
    from app.core.matrix import credit_fee as _qr_credit_fee
    from decimal import Decimal as _QRDec, ROUND_HALF_UP as _QR_ROUND
    from app.pix.service import get_available_balance as _qr_avail_bal
    _qr_fee_amount = float(_qr_fee_calc(sender.cpf_cnpj, payment_value, is_external=True, is_received=False))
    _qr_total_required = Decimal(str(payment_value)) + Decimal(str(_qr_fee_amount))

    # Available balance considers pending outbound (PROCESSING) transactions
    _qr_available = _qr_avail_bal(db, current_user.id)
    if _qr_available < _qr_total_required:
        # Insufficient balance after dispatch — mark as FAILED
        pix.status = PixStatus.FAILED
        db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                f"Saldo insuficiente. Disponivel: R$ {_qr_available:.2f}, "
                f"Necessario: R$ {_qr_total_required:.2f} "
                f"(valor R$ {payment_value:.2f} + taxa R$ {_qr_fee_amount:.2f})"
            )
        )

    # ── Update the pre-persisted transaction with Asaas response data ──
    payment_id = result.get("payment_id") or _provisional_id
    asaas_status = result.get("status", "BANK_PROCESSING")

    # If Asaas returned a different payment_id, update the transaction PK
    if payment_id != _provisional_id:
        pix.id = payment_id

    pix.value = payment_value if payment_value > 0 else 0.01
    pix.fee_amount = _qr_fee_amount
    pix.status = PixStatus.PROCESSING
    pix.correlation_id = result.get("end_to_end_id") or correlation_id
    pix.recipient_name = result.get("receiver_name")

    # Deferred debit: balance is NOT debited here. Debit happens at webhook
    # TRANSFER_DONE confirmation. A PENDING ledger entry is created for
    # auditability and get_available_balance() prevents overdraw.
    if payment_value > 0:
        _qr_ledger(
            db=db,
            account_id=str(current_user.id),
            entry_type=_QR_LET.DEBIT,
            amount=_qr_total_required,
            tx_id=pix.id,
            description=f"PIX QR outbound pending: {pix.id}",
        )
        logger.info(
            f"QR Code payment dispatched (deferred debit): id={pix.id}, user={sender.id}, "
            f"amount=R${payment_value:.2f}, fee=R${_qr_fee_amount:.2f}, total=R${_qr_total_required:.2f}, "
            f"available_balance=R${_qr_available:.2f}"
        )

    db.commit()
    db.refresh(pix)

    audit_log(
        action="PIX_QRCODE_PAYMENT",
        user=str(current_user.id),
        resource=f"payment_id={payment_id}",
        details={
            "payment_id": payment_id,
            "value": payment_value,
            "status": asaas_status,
            "receiver_name": result.get("receiver_name", "")
        }
    )

    result_dict = build_pix_response(pix, db).model_dump()
    if result.get("receiver_name"):
        result_dict["receiver_name"] = result["receiver_name"]
    return result_dict


@router.post("/webhook/asaas", status_code=200)
async def asaas_webhook(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
    x_correlation_id: str = Header(default=None)
):
    """
    Asaas payment webhook receiver.
    Auto-confirms charges when Asaas notifies PAYMENT_RECEIVED or PAYMENT_CONFIRMED.
    Configure in Asaas dashboard: Settings > Integrations > Webhooks > URL: /pix/webhook/asaas
    Token must match ASAAS_WEBHOOK_TOKEN environment variable.
    """
    from uuid import uuid4 as _uuid4
    from app.core.config import settings as _settings

    correlation_id = x_correlation_id or str(_uuid4())
    logger = get_logger_with_correlation(correlation_id)

    # Validate Asaas authentication token (header: asaas-access-token).
    # SECURITY INVARIANT: if ASAAS_WEBHOOK_TOKEN is not configured, ALL webhook
    # calls are rejected. Accepting unauthenticated webhooks would allow fake
    # payment confirmation that credits user balances without a real Asaas payment.
    if not _settings.ASAAS_WEBHOOK_TOKEN:
        logger.error(
            "[webhook/security] ASAAS_WEBHOOK_TOKEN not configured. "
            "All webhook calls rejected to prevent fake balance injection. "
            f"Origin: {request.client.host if request.client else 'unknown'}"
        )
        return {"received": False, "action": "rejected", "reason": "webhook_token_not_configured"}

    incoming_token = request.headers.get("asaas-access-token", "")
    if not incoming_token or not hmac.compare_digest(incoming_token, _settings.ASAAS_WEBHOOK_TOKEN):
        logger.warning(
            f"Asaas webhook rejected: invalid token. "
            f"Origin: {request.client.host if request.client else 'unknown'}"
        )
        # Return 200 to avoid Asaas retry storm, but take no action
        return {"received": False, "action": "rejected", "reason": "invalid_token"}

    event = payload.get("event", "")
    payment = payload.get("payment", {})
    payment_id = payment.get("id")

    logger.info(f"Asaas webhook received: event={event}, payment_id={payment_id}")

    try:
        return _process_asaas_webhook_event(event, payment, payment_id, db, logger)
    except Exception as _exc:
        logger.error(
            f"[webhook/unhandled] Exception processing event={event} payment_id={payment_id}: "
            f"{type(_exc).__name__}: {_exc}",
            exc_info=True,
        )
        # Return 500 so Asaas retries the webhook delivery.
        # Without retry, failed payment credits would be permanently lost.
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=500,
            content={"received": False, "action": "error", "reason": "internal_processing_error"},
        )


def _process_asaas_webhook_event(event: str, payment: dict, payment_id, db, logger):
    """Isolated processing logic — called by the webhook handler after auth.
    Keeping this separate allows the handler to catch all exceptions and return
    500, triggering Asaas webhook retry for failed payment processing.
    """
    # Handled events
    HANDLED_EVENTS = {
        "PAYMENT_RECEIVED",
        "PAYMENT_CONFIRMED",
        "TRANSFER_DONE",
        "TRANSFER_FAILED",
        "PAYMENT_REFUNDED",
        "PAYMENT_OVERDUE",
        "PAYMENT_DELETED",
        "PAYMENT_RESTORED",
    }
    if event not in HANDLED_EVENTS:
        return {"received": True, "action": "ignored", "event": event}

    # Transfer status events: update PixTransaction status
    if event in ("TRANSFER_DONE", "TRANSFER_FAILED"):
        transfer_id = payment.get("id") or payment.get("transferId")
        if transfer_id:
            # SELECT FOR UPDATE on the transaction row itself to prevent
            # concurrent webhook reprocessing (Asaas retries).
            pix_tx = db.query(PixTransaction).filter(
                PixTransaction.id == transfer_id
            ).with_for_update().first()
            if pix_tx:
                from app.pix.service import settle_ledger_entries, reverse_ledger_entries
                from app.core.fees import PLATFORM_PIX_OUTBOUND_NETWORK_FEE as _WH_NET_FEE
                from app.core.matrix import credit_fee as _wh_credit_fee
                from decimal import ROUND_HALF_UP as _WH_ROUND

                _wh_previous_status = pix_tx.status

                if event == "TRANSFER_DONE":
                    # Idempotency: if already CONFIRMED, this is a duplicate webhook.
                    # STOP immediately — never debit twice.
                    if pix_tx.status == PixStatus.CONFIRMED:
                        logger.info(
                            f"TRANSFER_DONE idempotent skip: tx={pix_tx.id} already CONFIRMED. "
                            f"Duplicate webhook ignored. transfer_id={transfer_id}"
                        )
                        db.commit()
                        return {"received": True, "action": "already_confirmed", "event": event}

                    # State machine: only PROCESSING -> CONFIRMED is valid
                    if pix_tx.status != PixStatus.PROCESSING:
                        logger.warning(
                            f"TRANSFER_DONE invalid transition: tx={pix_tx.id} "
                            f"status={pix_tx.status.value} is not PROCESSING. "
                            f"Event ignored. transfer_id={transfer_id}"
                        )
                        audit_log(
                            action="webhook_invalid_transition",
                            user=str(pix_tx.user_id),
                            resource=f"pix_id={pix_tx.id}",
                            details={
                                "event": event,
                                "current_status": pix_tx.status.value,
                                "expected_status": "PROCESSANDO",
                                "transfer_id": transfer_id,
                            }
                        )
                        db.commit()
                        return {"received": True, "action": "invalid_transition", "event": event}

                    pix_tx.status = PixStatus.CONFIRMED

                    # Deferred debit: balance is debited NOW at confirmation time.
                    # SELECT FOR UPDATE prevents concurrent balance mutation.
                    if pix_tx.type == TransactionType.SENT:
                        sender = db.query(User).filter(
                            User.id == pix_tx.user_id
                        ).with_for_update().first()
                        if sender:
                            fee_amount = Decimal(str(pix_tx.fee_amount or 0))
                            total_debit = Decimal(str(pix_tx.value)) + fee_amount
                            previous = sender.balance
                            sender.balance = Decimal(str(sender.balance)) - total_debit
                            db.add(sender)

                            if sender.balance < 0:
                                logger.critical(
                                    f"BALANCE_NEGATIVE_POST_CONFIRM: user={sender.id} "
                                    f"balance={sender.balance:.2f} after debit of "
                                    f"R${total_debit:.2f} (transfer {transfer_id}). "
                                    "Asaas transfer already completed — debit forced."
                                )

                            # Credit service margin to Matrix
                            _wh_fee_dec = fee_amount
                            _wh_svc_margin = (_wh_fee_dec - _WH_NET_FEE).quantize(
                                Decimal("0.01"), rounding=_WH_ROUND
                            )
                            if _wh_svc_margin > Decimal("0.00"):
                                _wh_credit_fee(db, float(_wh_svc_margin))

                            logger.info(
                                f"TRANSFER_DONE debit: user={sender.id}, "
                                f"value=R${pix_tx.value:.2f}, fee=R${fee_amount:.2f}, "
                                f"total=R${total_debit:.2f}, "
                                f"balance: R${previous:.2f} -> R${sender.balance:.2f}"
                            )
                            audit_log(
                                action="transfer_done_debit",
                                user=sender.id,
                                resource=f"pix_id={pix_tx.id}",
                                details={
                                    "amount": float(pix_tx.value),
                                    "fee_amount": float(fee_amount),
                                    "total_debit": float(total_debit),
                                    "previous_balance": float(previous),
                                    "new_balance": float(sender.balance),
                                    "transfer_id": transfer_id,
                                }
                            )

                    # Settle ledger entries for this transaction
                    settled_count = settle_ledger_entries(db, pix_tx.id)
                    logger.info(f"TRANSFER_DONE: settled {settled_count} ledger entries for tx={pix_tx.id}")

                    # Enrich recipient_name from Asaas transfer details when missing
                    _FALLBACK_NAMES = {None, "", "Destinatario nao identificado", "Destinatario externo"}
                    if pix_tx.recipient_name in _FALLBACK_NAMES:
                        try:
                            gw = get_payment_gateway()
                            if gw:
                                transfer_details = gw.get_payment_status(transfer_id)
                                resolved_name = transfer_details.get("receiver_name")
                                if resolved_name:
                                    pix_tx.recipient_name = resolved_name
                                    logger.info(
                                        f"TRANSFER_DONE enrichment: pix_id={pix_tx.id} "
                                        f"recipient_name resolved to '{resolved_name}'"
                                    )
                        except Exception as _enrich_err:
                            logger.warning(
                                f"TRANSFER_DONE enrichment failed for pix_id={pix_tx.id}: {_enrich_err}"
                            )
                else:
                    # TRANSFER_FAILED: Asaas rejected/refunded the transfer.

                    # Idempotency: if already FAILED, this is a duplicate webhook.
                    if pix_tx.status == PixStatus.FAILED:
                        logger.info(
                            f"TRANSFER_FAILED idempotent skip: tx={pix_tx.id} already FAILED. "
                            f"Duplicate webhook ignored. transfer_id={transfer_id}"
                        )
                        db.commit()
                        return {"received": True, "action": "already_failed", "event": event}

                    # State machine: only PROCESSING -> FAILED is valid
                    if pix_tx.status != PixStatus.PROCESSING:
                        logger.warning(
                            f"TRANSFER_FAILED invalid transition: tx={pix_tx.id} "
                            f"status={pix_tx.status.value} is not PROCESSING. "
                            f"Event ignored. transfer_id={transfer_id}"
                        )
                        audit_log(
                            action="webhook_invalid_transition",
                            user=str(pix_tx.user_id),
                            resource=f"pix_id={pix_tx.id}",
                            details={
                                "event": event,
                                "current_status": pix_tx.status.value,
                                "expected_status": "PROCESSANDO",
                                "transfer_id": transfer_id,
                            }
                        )
                        db.commit()
                        return {"received": True, "action": "invalid_transition", "event": event}

                    # Deferred debit model: balance was NOT debited at dispatch time.
                    # Only reverse PENDING ledger entries — no balance mutation needed.
                    pix_tx.status = PixStatus.FAILED
                    reversed_count = reverse_ledger_entries(db, pix_tx.id)
                    logger.info(
                        f"TRANSFER_FAILED: reversed {reversed_count} ledger entries for tx={pix_tx.id}, "
                        f"previous_status={_wh_previous_status.value}, transfer_id={transfer_id}"
                    )
                    audit_log(
                        action="transfer_failed_compensation",
                        user=str(pix_tx.user_id),
                        resource=f"pix_id={pix_tx.id}",
                        details={
                            "amount": float(pix_tx.value),
                            "fee_amount": float(pix_tx.fee_amount or 0),
                            "ledger_entries_reversed": reversed_count,
                            "transfer_id": transfer_id,
                            "previous_status": _wh_previous_status.value,
                        }
                    )
                db.add(pix_tx)
                db.commit()
                logger.info(
                    f"Asaas webhook: transfer {transfer_id} "
                    f"status {_wh_previous_status.value} -> {pix_tx.status.value} ({event})"
                )
        return {"received": True, "action": "transfer_updated", "event": event}

    # Refund / overdue / deleted / restored: log only, no balance mutation
    if event in ("PAYMENT_REFUNDED", "PAYMENT_OVERDUE", "PAYMENT_DELETED", "PAYMENT_RESTORED"):
        logger.info(f"Asaas webhook: lifecycle event {event} for payment {payment_id}")
        return {"received": True, "action": "logged", "event": event}

    # Only credit balance for confirmed/received events below
    if event not in ("PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"):
        return {"received": True, "action": "ignored", "event": event}

    if not payment_id:
        logger.warning("Asaas webhook: missing payment ID in payload")
        return {"received": True, "action": "ignored", "reason": "no_payment_id"}

    # Find pending charge in DB
    pix = db.query(PixTransaction).filter(
        PixTransaction.id == payment_id
    ).first()

    if not pix:
        # -----------------------------------------------------------------------
        # Inbound PIX via virtual key (not a charge we created).
        # This happens when an external bank user sends PIX directly to one of
        # the user's virtual keys (pix_random_key or pix_email_key).
        # Flow:
        #   1. External sender types user's virtual key in their bank app.
        #   2. Their bank routes the PIX to the platform's Asaas gateway key.
        #   3. Asaas fires PAYMENT_RECEIVED; pixTransaction.pixKey = destination key.
        #   4. We match that key to a user and credit their account (net of fee).
        #
        # NOTE: Asaas may send pixTransaction as a plain string UUID (the SPI
        # transaction ID) rather than an object. Guard against AttributeError.
        # -----------------------------------------------------------------------
        pix_transaction_raw = payment.get("pixTransaction")
        pix_transaction_data = pix_transaction_raw if isinstance(pix_transaction_raw, dict) else {}

        # DEBUG: log the raw payload fields to understand what Asaas is sending.
        # pixTransaction type and available keys are critical for resolver diagnostics.
        logger.info(
            f"[webhook/inbound-debug] payment_id={payment_id} "
            f"pixTransaction_type={type(pix_transaction_raw).__name__} "
            f"pixTransaction_raw={str(pix_transaction_raw)[:120] if not isinstance(pix_transaction_raw, dict) else 'dict'} "
            f"pixTransaction_keys={list(pix_transaction_data.keys()) if pix_transaction_data else []} "
            f"payment_keys={list(payment.keys())}"
        )

        dest_key   = (pix_transaction_data.get("pixKey") or "").strip().lower()
        payer_name = (
            pix_transaction_data.get("payerName")
            or payment.get("customerName")
            or "Pagador externo"
        )
        # Sender's CPF/CNPJ (raw digits) — used for self-deposit identification
        _raw_doc = lambda s: re.sub(r"\D", "", s or "")
        payer_doc = _raw_doc(
            pix_transaction_data.get("payerDocument")
            or pix_transaction_data.get("payerCpfCnpj")
            or (payment.get("payer") or {}).get("cpfCnpj")
            or payment.get("payerCpfCnpj")
            or ""
        )

        # Asaas customer ID — used as fallback resolution layer when CPF/CNPJ is
        # missing or masked.  The 'customer' field is present in most Asaas
        # PAYMENT_RECEIVED payloads and maps to asaas_customer_id on User.
        _asaas_customer_id = (payment.get("customer") or "").strip()

        # -----------------------------------------------------------------------
        # API FALLBACK: when pixTransaction is a string (Asaas sends only the
        # endToEnd/SPI transaction ID instead of a full object), OR when
        # payer_doc is still empty after parsing the webhook payload, we call
        # the Asaas REST API synchronously to fetch the full payment object.
        # This is necessary to recover payerDocument for CPF/CNPJ resolution.
        # -----------------------------------------------------------------------
        # Platform key fast path: payment sent directly to the shared deposit key.
        # When payer_doc is already available, skip API fallback entirely.
        # This eliminates up to 10s of latency for the most common deposit path.
        _is_platform_deposit = (dest_key == _PLATFORM_PIX_KEY)
        if _is_platform_deposit:
            logger.info(
                f"[webhook/platform-key] Deposit to shared wallet key detected. "
                f"payer_doc={'present' if payer_doc else 'missing'}, value={payment.get('value')}"
            )

        if (not payer_doc or not dest_key) and not (_is_platform_deposit and payer_doc) and payment_id and _settings.ASAAS_API_KEY:
            try:
                import httpx as _httpx
                _base = (
                    "https://sandbox.asaas.com/api/v3"
                    if _settings.ASAAS_USE_SANDBOX
                    else "https://api.asaas.com/v3"
                )
                _resp = _httpx.get(
                    f"{_base}/payments/{payment_id}",
                    headers={"access_token": _settings.ASAAS_API_KEY},
                    timeout=3.0,
                )
                if _resp.status_code == 200:
                    _full = _resp.json()
                    _pix_full = _full.get("pixTransaction") or {}
                    if isinstance(_pix_full, dict):
                        if not dest_key:
                            dest_key = (_pix_full.get("pixKey") or "").strip().lower()
                        if not payer_doc:
                            payer_doc = _raw_doc(
                                _pix_full.get("payerDocument")
                                or _pix_full.get("payerCpfCnpj")
                                or ""
                            )
                        if payer_name == "Pagador externo":
                            payer_name = _pix_full.get("payerName") or payer_name
                    elif isinstance(_pix_full, str) and _pix_full:
                        # Asaas sends pixTransaction as a SPI transaction UUID string
                        # (not a dict) for inbound deposits via virtual key.
                        # Call /pix/transactions/{uuid} to retrieve externalAccount data
                        # which carries the sender's masked CPF and name.
                        try:
                            _tx_resp = _httpx.get(
                                f"{_base}/pix/transactions/{_pix_full}",
                                headers={"access_token": _settings.ASAAS_API_KEY},
                                timeout=3.0,
                            )
                            if _tx_resp.status_code == 200:
                                _tx_data = _tx_resp.json()
                                _ext = _tx_data.get("externalAccount") or {}
                                if payer_name == "Pagador externo":
                                    payer_name = (_ext.get("name") or payer_name).strip()
                                # externalAccount.cpfCnpj is masked by Asaas: ***.XXX.XXX-**
                                # Store it for partial-match resolution in layer 4 below.
                                pix_transaction_data["_ext_masked_cpf"] = _ext.get("cpfCnpj") or ""
                                pix_transaction_data["_ext_name"] = payer_name
                            else:
                                logger.warning(
                                    f"[webhook/api-fallback] /pix/transactions/{_pix_full[:12]} "
                                    f"returned {_tx_resp.status_code}"
                                )
                        except Exception as _tx_exc:
                            logger.warning(
                                f"[webhook/api-fallback] Failed to fetch pix/transactions/{_pix_full[:12]}: "
                                f"{type(_tx_exc).__name__}: {_tx_exc}"
                            )
                    # Top-level payment fields may carry payer CPF/CNPJ even
                    # when pixTransaction does not (e.g. direct deposit to shared key).
                    if not payer_doc:
                        payer_doc = _raw_doc(
                            _full.get("payerCpfCnpj")
                            or (_full.get("payer") or {}).get("cpfCnpj")
                            or ""
                        )
                    if not _asaas_customer_id:
                        _asaas_customer_id = (_full.get("customer") or "").strip()
                    logger.info(
                        f"[webhook/api-fallback] Fetched payment {payment_id}: "
                        f"dest_key={dest_key!r} payer_doc={'*' * len(payer_doc)} "
                        f"asaas_cust={_asaas_customer_id!r} "
                        f"pix_type={type(_pix_full).__name__}"
                    )
                else:
                    logger.warning(
                        f"[webhook/api-fallback] Asaas returned {_resp.status_code} "
                        f"for payment {payment_id}"
                    )
            except Exception as _api_exc:
                logger.warning(
                    f"[webhook/api-fallback] Failed to fetch payment {payment_id}: "
                    f"{type(_api_exc).__name__}: {_api_exc}"
                )

        raw_value  = payment.get("value") or pix_transaction_data.get("value") or 0
        try:
            inbound_value = float(raw_value)
        except (TypeError, ValueError):
            inbound_value = 0.0

        logger.info(
            f"[webhook/resolver] payment_id={payment_id} "
            f"dest_key={dest_key!r} "
            f"payer_doc_len={len(payer_doc)} "
            f"asaas_cust={_asaas_customer_id!r} "
            f"inbound_value={inbound_value}"
        )

        if inbound_value > 0:
            # Resolution order:
            # 0. Platform deposit fast path: dest_key is the shared wallet key.
            #    Skip virtual-key lookup; go straight to payer CPF/CNPJ match.
            # 1. Virtual key match (pix_random_key or pix_email_key) — indexed, O(1)
            # 2. CPF/CNPJ key type — indexed O(1), no fallback scan
            # 3. Payer CPF/CNPJ match — indexed O(1), no fallback scan
            # 3.5. Asaas customer ID match — indexed, O(1)
            # 4. Masked CPF — SQL LIKE pattern, no full table scan in Python
            recipient_user: User | None = None

            if not _is_platform_deposit and dest_key:
                recipient_user = db.query(User).filter(
                    (User.pix_random_key == dest_key) | (User.pix_email_key == dest_key)
                ).first()

            if not recipient_user and not _is_platform_deposit and dest_key:
                dest_digits = _raw_doc(dest_key)
                if len(dest_digits) in (11, 14):
                    # Single indexed query — cpf_cnpj has unique index, O(1)
                    recipient_user = db.query(User).filter(User.cpf_cnpj == dest_digits).first()

            if not recipient_user and payer_doc and len(payer_doc) in (11, 14):
                # Layer 3: single indexed query — no fallback full scan
                recipient_user = db.query(User).filter(User.cpf_cnpj == payer_doc).first()

            # Layer 3.5: Asaas customer ID — when payer_doc is absent or masked,
            # the Asaas customer ID on the payment may still resolve the user.
            if not recipient_user and _asaas_customer_id:
                recipient_user = db.query(User).filter(
                    User.asaas_customer_id == _asaas_customer_id
                ).first()
                if recipient_user:
                    logger.info(
                        f"[webhook/resolver] layer3.5 asaas_customer_id match: "
                        f"user={recipient_user.id} cust={_asaas_customer_id}"
                    )

            # Layer 4: partial match via Asaas-masked CPF (e.g. ***.602.688-**)
            # SQL LIKE pattern pushed to PostgreSQL — no Python-level scan.
            # INDEX HINT: CREATE INDEX idx_users_cpf_cnpj_trgm ON users USING gin (cpf_cnpj gin_trgm_ops);
            # Pattern: "***602688**" → DB LIKE "___602688__" (11 chars, * → _)
            if not recipient_user:
                _masked = pix_transaction_data.get("_ext_masked_cpf") or ""
                if _masked:
                    import re as _re2
                    _mask_strip = _re2.sub(r"[.\-\s]", "", _masked)  # e.g. "***602688**"
                    if len(_mask_strip) == 11:
                        _like_pattern = _mask_strip.replace("*", "_")  # SQL LIKE wildcards
                        recipient_user = (
                            db.query(User)
                            .filter(User.cpf_cnpj.like(_like_pattern))
                            .first()
                        )
                        if recipient_user:
                            logger.info(
                                f"[webhook/resolver] layer4 masked-CPF match: "
                                f"user={recipient_user.id} mask={_masked}"
                            )

            if recipient_user:
                previous_bal = recipient_user.balance
                net_credit, fee_float = credit_pix_receipt(
                    db, recipient_user, inbound_value,
                    source=f"webhook_inbound:payment_id={payment_id}",
                )

                _eff_key = dest_key or payer_doc or "unknown"
                _eff_key_digits = _raw_doc(_eff_key)
                _eff_key_type = (
                    "ALEATORIA" if (len(_eff_key) == 36 and "-" in _eff_key)
                    else "CPF" if len(_eff_key_digits) == 11
                    else "CNPJ" if len(_eff_key_digits) == 14
                    else "EMAIL"
                )
                new_tx = PixTransaction(
                    id=payment_id,
                    value=net_credit,
                    pix_key=_eff_key,
                    key_type=_eff_key_type,
                    type=TransactionType.RECEIVED,
                    status=PixStatus.CONFIRMED,
                    user_id=recipient_user.id,
                    idempotency_key=f"inbound-{payment_id}",
                    description=f"PIX recebido de {payer_name}",
                    recipient_name=payer_name,
                    fee_amount=fee_float,
                )
                db.add(new_tx)
                db.commit()

                logger.info(
                    f"Inbound PIX credited: user={recipient_user.id} "
                    f"key_type={_eff_key_type} gross=R${inbound_value:.2f} "
                    f"fee=R${fee_float:.2f} net=R${net_credit:.2f} "
                    f"balance: R${previous_bal:.2f} -> R${recipient_user.balance:.2f}"
                )
                audit_log(
                    action="PIX_INBOUND_EXTERNAL",
                    user=recipient_user.id,
                    resource=f"payment_id={payment_id}",
                    details={
                        "dest_key": _eff_key,
                        "payer_name": payer_name,
                        "gross_value": inbound_value,
                        "fee": fee_float,
                        "net_credit": net_credit,
                        "balance_after": recipient_user.balance,
                    },
                )
                return {
                    "received": True,
                    "action": "inbound_key_credited",
                    "user_id": recipient_user.id,
                    "net_credit": net_credit,
                }

        logger.warning(
            f"Asaas webhook: payment {payment_id} not found in DB and no matching user "
            f"(dest_key={dest_key!r} payer_doc={'*' * len(payer_doc)} "
            f"asaas_cust={_asaas_customer_id!r}). Ignored."
        )
        audit_log(
            action="PIX_INBOUND_UNCLAIMED",
            user="system",
            resource=f"payment_id={payment_id}",
            details={
                "dest_key": dest_key,
                "payer_doc_masked": f"{'*' * len(payer_doc)}" if payer_doc else "unknown",
                "asaas_customer_id": _asaas_customer_id,
                "payer_name": payer_name,
                "inbound_value": str(inbound_value),
                "note": "No user matched. Admin manual credit required.",
            },
        )
        return {"received": True, "action": "ignored", "reason": "no_matching_user"}

    if pix.status.value == "CONFIRMADO":
        logger.info(f"Asaas webhook: charge already confirmed: {payment_id}")
        return {"received": True, "action": "already_confirmed"}

    # Confirm the transaction and credit balance
    pix.status = PixStatus.CONFIRMED

    # Extract payer name from webhook payload — Asaas sends customerName for paid charges
    payer_name = (
        payment.get("customerName")
        or (payment.get("pix") or {}).get("payerName")
    )
    if payer_name and not pix.recipient_name:
        pix.recipient_name = payer_name

    db.add(pix)

    receiver_user = db.query(User).filter(User.id == pix.user_id).first()
    if receiver_user:
        credit_pix_receipt(
            db, receiver_user, float(pix.value),
            source=f"webhook_charge_confirm:payment_id={payment_id}",
        )

    db.commit()

    logger.info(f"Asaas webhook: charge {payment_id} confirmed automatically via webhook")
    return {"received": True, "action": "confirmed", "charge_id": payment_id}


@router.post("/webhook/asaas/validacao-saque", status_code=200)
async def asaas_withdrawal_validation(
    request: Request,
    x_correlation_id: str = Header(default=None)
):
    """
    Asaas withdrawal validation webhook.
    Receives a withdrawal request from Asaas and approves it instantly.
    Configure in Asaas: Mecanismos de seguranca > Validacao de saque > URL.
    URL: <APP_BASE_URL>/pix/webhook/asaas/validacao-saque
    Optional token: ASAAS_WITHDRAWAL_VALIDATION_TOKEN environment variable.
    """
    from uuid import uuid4 as _uuid4
    from app.core.config import settings as _settings

    correlation_id = x_correlation_id or str(_uuid4())
    logger = get_logger_with_correlation(correlation_id)

    # Validate authentication token — MANDATORY for security.
    # If ASAAS_WITHDRAWAL_VALIDATION_TOKEN is not configured, reject ALL requests
    # to prevent unauthorized withdrawals from the Asaas master account.
    if not _settings.ASAAS_WITHDRAWAL_VALIDATION_TOKEN:
        logger.error(
            "Withdrawal validation rejected: ASAAS_WITHDRAWAL_VALIDATION_TOKEN not configured. "
            "All withdrawal validations are refused until token is set."
        )
        return {"status": "REFUSED", "refuseReason": "Webhook token not configured"}

    incoming_token = request.headers.get("asaas-access-token", "")
    if not incoming_token or not hmac.compare_digest(incoming_token, _settings.ASAAS_WITHDRAWAL_VALIDATION_TOKEN):
        logger.warning(
            f"Withdrawal validation rejected: invalid token. "
            f"Origin: {request.client.host if request.client else 'unknown'}"
        )
        return {"status": "REFUSED", "refuseReason": "Unauthorized request"}

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # Asaas payload has "type" at root; transfer data nested by type
    withdrawal_type = payload.get("type", "")
    nested = (
        payload.get("transfer")
        or payload.get("bill")
        or payload.get("pixQrCode")
        or payload.get("mobilePhoneRecharge")
        or payload.get("pixRefund")
        or {}
    )
    withdrawal_id = nested.get("id", payload.get("id", "unknown"))
    withdrawal_value = nested.get("value", payload.get("value", 0))

    # ---- Validation rules ----
    # 1. Reject negative or zero values
    try:
        _wval = float(withdrawal_value)
    except (TypeError, ValueError):
        _wval = 0.0

    if _wval <= 0:
        logger.warning(
            f"Withdrawal REFUSED: invalid value={withdrawal_value}, "
            f"type={withdrawal_type}, id={withdrawal_id}"
        )
        return {"status": "REFUSED", "refuseReason": "Valor invalido"}

    # 2. Enforce maximum single-withdrawal limit (R$ 50,000)
    _MAX_WITHDRAWAL = 50_000.0
    if _wval > _MAX_WITHDRAWAL:
        logger.warning(
            f"Withdrawal REFUSED: value=R${_wval} exceeds limit R${_MAX_WITHDRAWAL}, "
            f"type={withdrawal_type}, id={withdrawal_id}"
        )
        return {
            "status": "REFUSED",
            "refuseReason": f"Valor excede limite de R$ {_MAX_WITHDRAWAL:,.2f}"
        }

    # 3. Reject unknown/unsupported withdrawal types (allow only known categories)
    _ALLOWED_TYPES = {"TRANSFER", "PIX", "BILL_PAYMENT", "PIX_REFUND"}
    if withdrawal_type and withdrawal_type not in _ALLOWED_TYPES:
        logger.warning(
            f"Withdrawal REFUSED: unsupported type={withdrawal_type}, "
            f"id={withdrawal_id}, value=R${_wval}"
        )
        return {"status": "REFUSED", "refuseReason": f"Tipo nao suportado: {withdrawal_type}"}

    logger.info(
        f"Asaas withdrawal validation: type={withdrawal_type}, id={withdrawal_id}, "
        f"value=R${_wval} -> APPROVED"
    )

    return {"status": "APPROVED"}


# ===========================================================================
# PIX KEY MANAGEMENT — /pix/minhas-chaves
# Virtual keys that route inbound external PIX to the correct user account.
# The underlying gateway key is _PLATFORM_PIX_KEY (Asaas random key).
# ===========================================================================

@router.get("/minhas-chaves")
def get_minhas_chaves(
    current_user: User = Depends(get_current_user),
):
    """
    Returns the single shared deposit PIX key for this account.

    All accounts share the same platform deposit wallet. The system identifies
    the depositing user by the payer CPF/CNPJ sent in the Asaas webhook payload.
    """
    import re as _re
    raw = _re.sub(r"\D", "", current_user.cpf_cnpj or "")
    if len(raw) == 11:
        cpf_masked = f"***.{raw[3:6]}.{raw[6:9]}-**"
    elif len(raw) == 14:
        cpf_masked = f"**.***.{raw[5:8]}/{raw[8:12]}-**"
    else:
        cpf_masked = raw[:3] + "***" + raw[-2:] if len(raw) >= 5 else "***"
    return {
        "deposit_key": _SHARED_DEPOSIT_WALLET_ID,
        "deposit_key_type": "EVP",
        "your_identifier": cpf_masked,
        "instructions": (
            "Envie PIX da sua conta bancaria usando o CPF/CNPJ cadastrado como conta de origem. "
            "O sistema identifica voce automaticamente pelo documento do remetente."
        ),
    }


@router.post("/minhas-chaves/email", status_code=200)
def register_email_pix_key(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Registers the user's account email as their PIX email key.

    Rules:
    - Only the account email can be used (no arbitrary email input — prevents impersonation).
    - If another user already has this email as a PIX key, the operation is rejected.
    - Idempotent: calling again when already registered returns success.
    """
    email = current_user.email.strip().lower()

    if current_user.pix_email_key and current_user.pix_email_key.lower() == email:
        return {
            "status": "already_registered",
            "pix_email_key": current_user.pix_email_key,
            "message": "Chave email ja estava registrada.",
        }

    # Verify no other user holds this email key
    conflict = db.query(User).filter(
        User.pix_email_key == email,
        User.id != current_user.id,
    ).first()
    if conflict:
        raise HTTPException(
            status_code=409,
            detail="Este email ja esta registrado como chave PIX de outra conta.",
        )

    live_user = db.query(User).filter(User.id == current_user.id).first()
    if not live_user:
        raise HTTPException(status_code=404, detail="Conta nao encontrada.")

    live_user.pix_email_key = email
    db.add(live_user)
    db.commit()

    audit_log(
        action="PIX_EMAIL_KEY_REGISTERED",
        user=current_user.id,
        resource="pix_email_key",
        details={"email_key": email},
    )

    return {
        "status": "registered",
        "pix_email_key": email,
        "message": "Chave email PIX registrada com sucesso.",
    }


@router.delete("/minhas-chaves/email", status_code=200)
def remove_email_pix_key(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Removes the user's PIX email key.
    After removal, inbound PIX sent to that email will no longer credit this account.
    pix_random_key is unaffected.
    """
    if not current_user.pix_email_key:
        return {"status": "not_registered", "message": "Nenhuma chave email PIX registrada."}

    removed_key = current_user.pix_email_key

    live_user = db.query(User).filter(User.id == current_user.id).first()
    if live_user:
        live_user.pix_email_key = None
        db.add(live_user)
        db.commit()

    audit_log(
        action="PIX_EMAIL_KEY_REMOVED",
        user=current_user.id,
        resource="pix_email_key",
        details={"removed_key": removed_key},
    )

    return {
        "status": "removed",
        "message": "Chave email PIX removida. Sua Chave Aleatoria permanece ativa.",
    }


def build_pix_response(pix: Any, db: Session) -> PixResponse:
    """
    Constructs a PixResponse with enriched data (names, masked docs, formatted time).
    """
    # 2. Identify Sender and Receiver
    # Default values
    sender_name = "Unknown"
    sender_doc = "***"
    receiver_name = "Unknown"
    receiver_doc = "***"

    # Fetch the owner of this transaction record
    owner_user = db.query(User).filter(User.id == pix.user_id).first()

    if pix.type == TransactionType.SENT:
        # The owner is the sender
        if owner_user:
            sender_name = owner_user.name
            sender_doc = mask_cpf_cnpj(owner_user.cpf_cnpj)

        # Try to find the receiver via correlation_id (Internal Transfer)
        # Look for a RECEIVED transaction with same correlation_id
        receiver_tx = db.query(PixTransaction).filter(
            PixTransaction.correlation_id == pix.correlation_id,
            PixTransaction.type == TransactionType.RECEIVED
        ).first()

        if receiver_tx:
            receiver_user = db.query(User).filter(User.id == receiver_tx.user_id).first()
            if receiver_user:
                receiver_name = receiver_user.name
                receiver_doc = mask_cpf_cnpj(receiver_user.cpf_cnpj)
        else:
            # External or not found — use stored recipient name when available
            receiver_name = pix.recipient_name or "Destinatario externo"
            receiver_doc = mask_cpf_cnpj(pix.pix_key)  # Best effort

    elif pix.type == TransactionType.RECEIVED:
        # The owner is the receiver
        if owner_user:
            receiver_name = owner_user.name
            receiver_doc = mask_cpf_cnpj(owner_user.cpf_cnpj)

        # Try to find the sender via correlation_id (Internal Transfer)
        # Look for an SENT transaction with same correlation_id
        sender_tx = db.query(PixTransaction).filter(
            PixTransaction.correlation_id == pix.correlation_id,
            PixTransaction.type == TransactionType.SENT
        ).first()

        if sender_tx:
            sender_user = db.query(User).filter(User.id == sender_tx.user_id).first()
            if sender_user:
                sender_name = sender_user.name
                sender_doc = mask_cpf_cnpj(sender_user.cpf_cnpj)
        else:
            # Deposito ou externo
            if "SIMULACAO" in pix.pix_key or "Deposit" in (pix.description or ""):
                sender_name = "Deposito via QR Code"
                sender_doc = "Instituicao Financeira"
            else:
                sender_name = pix.recipient_name or "Pagador externo"
                sender_doc = "***"

    return PixResponse(
        id=pix.id,
        value=pix.value,
        pix_key=pix.pix_key,
        key_type=pix.key_type,
        type=pix.type,
        status=pix.status,
        description=pix.description,
        scheduled_date=pix.scheduled_date,
        created_at=pix.created_at,
        updated_at=pix.updated_at,
        formatted_time=format_brasilia_time(pix.created_at),
        sender_name=sender_name,
        sender_doc=sender_doc,
        receiver_name=receiver_name,
        receiver_doc=receiver_doc,
        correlation_id=pix.correlation_id,
        fee_amount=pix.fee_amount if pix.fee_amount is not None else 0.0,
        fee_description=fee_display(Decimal(str(pix.fee_amount or 0))),
    )


# ============================================================================
# ASAAS INTEGRATION ENDPOINTS - Real PIX Operations
# ============================================================================

@router.post("/charges/create", response_model=Dict[str, Any], status_code=201)
def create_pix_charge_endpoint(
    value: float,
    description: str,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> Dict[str, Any]:
    """
    Creates a PIX charge (cobranca) with QR Code via Asaas.

    **Real Integration**: Generates actual PIX QR Code for payment collection.

    Args:
        value: Charge value in BRL (max R$ 1,000,000.00)
        description: Charge description (max 500 chars)

    Returns:
        {
            "charge_id": str,
            "qr_code": str,  # Copy-paste code
            "qr_code_url": str,  # Base64 QR Code image
            "value": float,
            "status": str,
            "created_at": datetime
        }
    """
    from app.pix.service import create_pix_charge_with_qrcode

    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        if value <= 0 or value > 1000000:
            raise HTTPException(status_code=400, detail="Value must be between 0.01 and 1,000,000.00")

        if not description or len(description) > 500:
            raise HTTPException(status_code=400, detail="Description is required and must be <= 500 chars")

        logger.info(f"Creating PIX charge for user {current_user.id}: value={value}, desc={description[:50]}")

        pix = create_pix_charge_with_qrcode(
            db=db,
            value=value,
            description=description,
            user_id=current_user.id,
            idempotency_key=x_idempotency_key,
            correlation_id=correlation_id
        )

        return {
            "charge_id": pix.id,
            "qr_code": pix.pix_key,  # QR Code copy-paste stored in pix_key
            "qr_code_url": None,  # TODO: Store QR Code image URL in database
            "value": pix.value,
            "status": pix.status.value,
            "created_at": pix.created_at
        }

    except ValueError as e:
        logger.warning(f"Validation error creating PIX charge: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating PIX charge: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error creating PIX charge")


@router.post("/payments/execute", response_model=Dict[str, Any])
def execute_pix_payment_endpoint(
    pix_transaction_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
) -> Dict[str, Any]:
    """
    Executes a PIX payment (transferencia) via Asaas.

    **Real Integration**: Submits actual PIX transfer to Asaas gateway.

    Args:
        pix_transaction_id: Local PIX transaction ID (must be CREATED status)

    Returns:
        {
            "payment_id": str,
            "status": str,
            "end_to_end_id": str,
            "submitted_at": datetime
        }
    """
    from app.pix.service import execute_pix_payment_real

    correlation_id = x_correlation_id or str(uuid4())
    logger = get_logger_with_correlation(correlation_id)

    try:
        # Fetch transaction
        pix = db.query(PixTransaction).filter(
            PixTransaction.id == pix_transaction_id,
            PixTransaction.user_id == current_user.id,
            PixTransaction.type == TransactionType.SENT
        ).first()

        if not pix:
            raise HTTPException(status_code=404, detail="PIX transaction not found or unauthorized")

        if pix.status != PixStatus.CREATED:
            raise HTTPException(
                status_code=400,
                detail=f"Transaction cannot be executed. Current status: {pix.status.value}"
            )

        logger.info(f"Executing PIX payment: id={pix_transaction_id}, value={pix.value}")

        success = execute_pix_payment_real(
            db=db,
            pix_transaction=pix,
            correlation_id=correlation_id
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to execute PIX payment via gateway")

        return {
            "payment_id": pix.id,
            "status": pix.status.value,
            "end_to_end_id": None,  # TODO: Store E2E ID from Asaas response
            "submitted_at": pix.updated_at
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing PIX payment: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error executing PIX payment")


@router.get("/charges/{charge_id}/sync", response_model=PixResponse)
def sync_pix_charge_status_endpoint(
    charge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> PixResponse:
    """
    Synchronizes PIX charge status with Asaas gateway.

    Fetches current status from Asaas and updates local database.

    Args:
        charge_id: PIX charge ID

    Returns:
        Updated transaction details
    """
    from app.pix.service import sync_pix_charge_status

    try:
        pix = sync_pix_charge_status(db, charge_id)

        if not pix:
            raise HTTPException(status_code=404, detail="PIX charge not found")

        if pix.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Unauthorized")

        return build_pix_response(pix, db)

    except HTTPException:
        raise
    except Exception as e:
        fallback_logger = get_logger_with_correlation("sync-status")
        fallback_logger.error(f"Error syncing PIX charge status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error syncing status")
