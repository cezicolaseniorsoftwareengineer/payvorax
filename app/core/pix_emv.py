"""
BR Code PIX EMV helpers — BACEN specification.

Extracted from pix/router.py for reuse across router, web_routes, and link page.
Functions are stateless and side-effect-free.
"""
import re
import urllib.parse
from typing import Optional


def crc16_ccitt(data: str) -> str:
    """
    CRC-16/CCITT-FALSE (polynomial 0x1021, init 0xFFFF).
    Required by BACEN BR Code PIX specification (section 4.1).
    Mandatory for interoperability — any PSP app validates this before querying DICT.
    """
    crc = 0xFFFF
    for byte in data.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return format(crc, "04X")


def _tlv(tag: str, value: str) -> str:
    """Encodes a single TLV field: tag(2) + length(2, zero-padded decimal) + value."""
    return f"{tag}{len(value):02d}{value}"


def build_pix_static_emv(charge_id: str, value: float) -> str:
    """
    Builds a valid BR Code PIX static EMV payload per BACEN specification.
    Uses the charge UUID as the EVP random key (field 26.01).

    The generated code is format-valid and CRC-valid — any PSP app will parse it without error.
    In production (gateway configured), Asaas charges replace this entirely with a real
    dynamic QR code registered at DICT/SPI. This fallback only applies when:
      - Asaas gateway is not configured (local/dev), OR
      - Asaas API fails for this specific request.
    """
    gui = _tlv("00", "BR.GOV.BCB.PIX")
    key = _tlv("01", charge_id)           # EVP key = charge UUID (unique per charge)
    merchant_account = _tlv("26", gui + key)

    # Field 62: Additional Data — txid max 25 chars (hyphens stripped per spec)
    txid = charge_id.replace("-", "")[:25]
    additional = _tlv("62", _tlv("05", txid))

    # Field 54: Transaction Amount — must be "10.00" decimal form, NOT "1000"
    amount_str = f"{value:.2f}"

    payload = (
        _tlv("00", "01") +               # Payload Format Indicator
        _tlv("01", "11") +               # Point of Initiation = 11 (single-use static)
        merchant_account +
        _tlv("52", "0000") +             # Merchant Category Code
        _tlv("53", "986") +              # Transaction Currency: BRL = 986
        _tlv("54", amount_str) +         # Transaction Amount
        _tlv("58", "BR") +               # Country Code
        _tlv("59", "BioCodeTechPay") +   # Merchant Name (max 25 chars)
        _tlv("60", "BRASILIA") +         # Merchant City (max 15 chars)
        additional +
        "6304"                           # CRC tag — checksum appended immediately below
    )

    return payload + crc16_ccitt(payload)


def build_pix_static_emv_no_amount(pix_key: str, merchant_name: str = "Bio Tech Pay") -> str:
    """
    Builds a BR Code PIX static EMV payload WITHOUT a transaction amount.

    The payer chooses the amount freely in their bank app — this is the correct
    format for a shared deposit key (chave aleatoria) where each sender enters
    their own amount. Omitting field 54 (Transaction Amount) is spec-compliant
    per BACEN BR Code section 4.3.1.

    Point of Initiation = 12 (multi-use static, no fixed amount).
    """
    gui = _tlv("00", "BR.GOV.BCB.PIX")
    key = _tlv("01", pix_key)
    merchant_account = _tlv("26", gui + key)

    # txid: hyphens stripped, max 25 chars per BR Code spec section 4.6.4
    txid = pix_key.replace("-", "")[:25]
    additional = _tlv("62", _tlv("05", txid))

    payload = (
        _tlv("00", "01") +                        # Payload Format Indicator
        _tlv("01", "12") +                        # Point of Initiation: 12 = multi-use, no fixed amount
        merchant_account +
        _tlv("52", "0000") +                      # Merchant Category Code
        _tlv("53", "986") +                       # Transaction Currency: BRL = 986
        # Field 54 (Transaction Amount) intentionally omitted
        _tlv("58", "BR") +                        # Country Code
        _tlv("59", merchant_name[:25]) +          # Merchant Name (max 25 chars)
        _tlv("60", "BRASILIA") +                  # Merchant City (max 15 chars)
        additional +
        "6304"                                    # CRC tag
    )

    return payload + crc16_ccitt(payload)


def build_qr_url(emv_payload: str, size: int = 400) -> str:
    """
    Returns the qrserver.com URL for rendering the EMV QR code image.

    Parameters chosen for BR Code / PIX interoperability with POS terminals:
    - size=400x400: sufficient pixel density for maquininha scanners at arm's length
    - ecc=H: error correction level H (30%), required by BACEN for PIX QR codes
    - margin=4: quiet zone of 4 modules minimum per ISO/IEC 18004 and BR Code spec
    """
    return (
        "https://api.qrserver.com/v1/create-qr-code/"
        f"?size={size}x{size}&ecc=H&margin=4&data={urllib.parse.quote(emv_payload)}"
    )


def _walk_tlv(data: str):
    """
    Generator that yields (tag, value) pairs from a flat EMV TLV string.
    Tags are 2-char, lengths are 2-digit decimal (BR Code / PIX spec).
    Stops silently on malformed input.
    """
    pos = 0
    while pos + 4 <= len(data):
        tag = data[pos:pos + 2]
        try:
            length = int(data[pos + 2:pos + 4])
        except ValueError:
            break
        if pos + 4 + length > len(data):
            break
        yield tag, data[pos + 4:pos + 4 + length]
        pos += 4 + length


_PAYLOAD_URL_DOMAIN_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/')


def _is_payload_url(value: str) -> bool:
    """
    Returns True when value looks like a payloadLocation URL rather than a Pix key.
    Dynamic QR codes from POS terminals (Stone, Cielo, PagSeguro, Mercado Pago)
    embed an https:// URL in field 26/sub-tag 01 instead of the actual Pix key.
    """
    if not value:
        return False
    if value.startswith(("https://", "http://", "pix://")):
        return True
    # Bare domain URL without scheme: e.g. pix.bb.com.br/cobv/...
    if "/" in value and value[0].isalpha() and _PAYLOAD_URL_DOMAIN_RE.match(value):
        return True
    return False


def _normalise_payload_url(url: str) -> str:
    url = url.strip()
    if url.startswith("pix://"):
        return "https://" + url[6:]
    if not url.startswith("http"):
        return "https://" + url
    return url


def parse_emv_pix_key(emv: str):
    """
    Extracts the Pix key and its type from a PIX EMV payload.

    Scans Merchant Account Info fields 26-51 (BACEN BR Code v2.1 allows any
    slot) for the first block whose GUI identifies it as a PIX block
    (contains 'BR.GOV.BCB.PIX'). Sub-tag 01 holds the static Pix key.

    Returns (pix_key, key_type) where key_type is one of:
    EMAIL, CPF, CNPJ, PHONE, EVP.

    Returns (None, None) if the key cannot be extracted OR if the block
    contains a payloadLocation URL in sub-tag 25 or sub-tag 01 (dynamic QR —
    use parse_emv_payload_url to resolve the actual Pix key from the PSP).
    """
    for tag, value in _walk_tlv(emv):
        try:
            tag_id = int(tag)
        except ValueError:
            continue
        if not (26 <= tag_id <= 51):
            continue

        sub: dict = {}
        for s_tag, s_val in _walk_tlv(value):
            sub[s_tag] = s_val

        gui = sub.get("00", "").upper()
        if "BCB.PIX" not in gui and "BR.GOV.BCB" not in gui:
            continue  # not a PIX block (VISA, MASTERCARD, etc.)

        # Sub-tag 25: canonical payloadLocation (BACEN spec) — dynamic QR, no static key here.
        val25 = sub.get("25", "")
        if val25 and "/" in val25:
            return None, None

        key = sub.get("01", "")
        if not key:
            continue

        # Dynamic QR: sub-tag 01 holds a payloadLocation URL, not a key.
        # Do NOT return the URL as a Pix key — it would corrupt /transfers.
        if _is_payload_url(key):
            return None, None

        if "@" in key:
            return key, "EMAIL"
        if re.match(r'^\d{14}$', key):
            return key, "CNPJ"
        if re.match(r'^\d{11}$', key):
            return key, "CPF"
        if key.startswith("+"):
            return key, "PHONE"
        # EVP (UUID random key) — verify format before trusting
        clean = key.replace("-", "")
        if re.match(r'^[0-9a-f]{32}$', clean, re.IGNORECASE):
            return key, "EVP"
        # Unknown short value — treat as EVP (DICT will reject if invalid)
        return key, "EVP"
    return None, None


def parse_emv_payload_url(emv: str) -> Optional[str]:
    """
    Extracts the payloadLocation URL from a dynamic BR Code QR EMV string.

    BACEN Manual BR Code v2.1 — dynamic QR codes from POS terminals embed the
    PSP charge URL in one of two positions inside the PIX Merchant Account block:
      - Sub-tag 25: canonical BACEN position (Stone, Cielo, Rede, Sicredi)
      - Sub-tag 01: used by older PagSeguro and Mercado Pago firmware

    The PIX block is identified by GUI sub-tag 00 containing 'BR.GOV.BCB.PIX'.
    Fields 26-51 are all valid Merchant Account Info slots per ABECS spec.

    Returns the normalised https:// URL, or None for static QR codes.
    """
    for tag, value in _walk_tlv(emv):
        try:
            tag_id = int(tag)
        except ValueError:
            continue
        if not (26 <= tag_id <= 51):
            continue

        # Parse sub-TLV to locate GUI and payloadLocation
        sub: dict = {}
        for s_tag, s_val in _walk_tlv(value):
            sub[s_tag] = s_val

        gui = sub.get("00", "").upper()
        if "BCB.PIX" not in gui and "BR.GOV.BCB" not in gui:
            continue  # not a PIX block (VISA, MASTERCARD, etc.)

        # Sub-tag 25: canonical payloadLocation (BACEN spec)
        val25 = sub.get("25", "")
        if val25 and "/" in val25:
            return _normalise_payload_url(val25)

        # Sub-tag 01: URL-valued (PagSeguro/Mercado Pago older firmware)
        val01 = sub.get("01", "")
        if val01 and _is_payload_url(val01):
            return _normalise_payload_url(val01)

    return None


def parse_emv_amount(emv: str) -> float:
    """
    Extracts the transaction amount from EMV field 54 (Transaction Amount).
    Returns 0.0 if field 54 is absent or unparseable.
    """
    for tag, value in _walk_tlv(emv):
        if tag == "54":
            try:
                return float(value)
            except ValueError:
                return 0.0
    return 0.0
