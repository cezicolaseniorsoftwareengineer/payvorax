"""
Transaction fee calculation engine — Bio Code Tech Pay.

Fee policy:
  Gateway costs (Asaas — confirmed 11/03/2026):
    - Every external PIX sent:     R$ 2.00  (first 100/month free)
    - Every external PIX received: R$ 1.99  (first 100/month free)
    - Every boleto paid:           R$ 1.99

  IMPORTANT: all costs above are passed through to the client WITH margin.
  Never set a client fee below the gateway cost — that creates structural loss.

  PF (CPF — 11 raw digits)
    - External PIX sent:     R$ 2.50 fixed  (R$2.00 Asaas + R$0.50 margin)
    - External PIX received: R$ 2.49 fixed  (R$1.99 Asaas + R$0.50 margin)
    - Boleto payment:        R$ 2.49 fixed  (R$1.99 Asaas + R$0.50 margin)
    - Internal transfer:     free  (no Asaas fee — stays within platform)

  PJ (CNPJ — 14 raw digits)
    - External PIX sent:     max(R$3.00, 0.8% of value)  (R$2.00 Asaas + R$1.00 margin min)
    - External PIX received: max(R$2.49, 0.495% of value) (R$1.99 Asaas + margin, rate for large values)
    - Boleto payment:        R$ 2.99 fixed  (R$1.99 Asaas + R$1.00 margin)
    - Internal transfer:     free

All internal transfers (Bio Code Tech Pay -> Bio Code Tech Pay) are always free.

Constants prefixed ASAAS_ document gateway costs at the time of measurement.
Update them whenever Asaas changes their pricing.
"""
from decimal import Decimal, ROUND_HALF_UP
import re


_TWO_PLACES = Decimal("0.01")

# ---------------------------------------------------------- Asaas gateway costs
# Confirmed from Asaas dashboard on 11/03/2026.
# First 100 outbound and 100 inbound PIX per month are free — steady-state
# cost applies at scale, so all fees are calculated at the non-free rate.
ASAAS_PIX_OUTBOUND_COST  = Decimal("2.00")   # per external PIX sent
ASAAS_PIX_INBOUND_COST   = Decimal("1.99")   # per external PIX received (after 100 free/mo)
ASAAS_BOLETO_COST        = Decimal("1.99")   # per boleto paid
ASAAS_PIX_FREE_MONTHLY   = 100               # free ops per month (both in and out)

# --------------------------------------------------------------------------- PF
_PIX_SENT_PF = Decimal("2.50")   # ASAAS_PIX_OUTBOUND_COST (R$2.00) + R$0.50 margin
_PIX_RECV_PF = Decimal("2.49")   # ASAAS_PIX_INBOUND_COST  (R$1.99) + R$0.50 margin
_BOLETO_PF   = Decimal("2.49")   # ASAAS_BOLETO_COST        (R$1.99) + R$0.50 margin

# --------------------------------------------------------------------------- PJ
_PIX_SENT_RATE_PJ  = Decimal("0.0080")   # 0.8% of value
_PIX_SENT_MIN_PJ   = Decimal("3.00")    # minimum: ASAAS_PIX_OUTBOUND_COST + R$1.00 margin
_PIX_RECV_RATE_PJ  = Decimal("0.00495") # 0.495% of value (scales on large transactions)
_PIX_RECV_MIN_PJ   = Decimal("2.49")    # minimum: ASAAS_PIX_INBOUND_COST + R$0.50 margin
_BOLETO_PJ         = Decimal("2.99")    # ASAAS_BOLETO_COST (R$1.99) + R$1.00 margin


def _raw_digits(cpf_cnpj) -> str:
    if not isinstance(cpf_cnpj, str):
        cpf_cnpj = str(cpf_cnpj) if cpf_cnpj is not None else ""
    return re.sub(r"\D", "", cpf_cnpj)


def is_pj(cpf_cnpj: str) -> bool:
    """Returns True when the document is a CNPJ (14 digits)."""
    return len(_raw_digits(cpf_cnpj)) == 14


def calculate_pix_fee(
    cpf_cnpj: str,
    amount: float,
    *,
    is_external: bool,
    is_received: bool = False,
) -> Decimal:
    """
    Calculates PIX transaction fee.

    Args:
        cpf_cnpj: Raw CPF or CNPJ string of the account holder.
        amount:   Transaction value in BRL.
        is_external: True for external (inter-bank) transfers; False for internal.
        is_received: True when the transaction is incoming (charge paid by third party).

    Returns:
        Fee amount as Decimal rounded to 2 decimal places.
    """
    if not is_external:
        return Decimal("0.00")

    value = Decimal(str(amount))

    if is_pj(cpf_cnpj):
        if is_received:
            fee = value * _PIX_RECV_RATE_PJ
            return max(fee, _PIX_RECV_MIN_PJ).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        fee = value * _PIX_SENT_RATE_PJ
        return max(fee, _PIX_SENT_MIN_PJ).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    else:
        return _PIX_RECV_PF if is_received else _PIX_SENT_PF


def calculate_boleto_fee(cpf_cnpj: str) -> Decimal:
    """Returns the fixed fee for boleto payments based on account type."""
    return _BOLETO_PJ if is_pj(cpf_cnpj) else _BOLETO_PF


def fee_display(fee: Decimal) -> str:
    """
    Formats a fee amount as a human-readable Brazilian Real string.
    Returns 'Gratuito' when zero.
    """
    if fee == Decimal("0.00"):
        return "Gratuito"
    formatted = f"{float(fee):.2f}".replace(".", ",")
    return f"R$ {formatted}"
