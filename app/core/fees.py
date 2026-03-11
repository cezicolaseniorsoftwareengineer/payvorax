"""
Transaction fee calculation engine — BioCodeTechPay.

Asaas gateway costs (verified from account statement on 11/03/2026):
  Inbound (cobranca/QR Code received):
    Gross cost: R$1.99 (pix fee) + R$0.99 (messaging fee) = R$2.98
    Net cost:   R$0.00  — Asaas fully discounts both fees on the current plan
                         (partial rollback detected at end of 11/03 cycle,
                          signals quota nearing exhaustion — treat as R$0.00 in
                          the steady-state model but monitor monthly volume)
    Implication: Inbound charges are ZERO-COST gateway operations.
                 Charging the platform client creates pure revenue with no
                 gateway liability. This is the healthiest monetisation path.

  Outbound (transfer sent via PIX key / copy-paste / QR scan):
    Within free monthly quota: R$0.00
    After quota:               R$2.00 flat per transfer (confirmed statement,
                               two transfers on 11/03 with explicit fee line)
    CRITICAL: Sending R$0.25 when Asaas charges R$2.00 = -R$1.75 structural loss.
              Platform minimum fee (R$3.00 PJ / R$2.50 PF) ALWAYS covers the
              R$2.00 Asaas cost — guaranteed minimum margin of R$1.00 (PJ)
              and R$0.50 (PF) per outbound transaction.
    Percentage crossover for PJ: at R$375 the rate 0.80% x 375 = R$3.00 matches
    the minimum; above R$375 the percentage result exceeds the flat floor,
    increasing the absolute margin proportionally.
    R$250 reference: 0.80% x 250 = R$2.00 = Asaas cost with NO floor applied —
    relevant only for theoretical flat-rate analysis, NOT the actual billing model.

  Boleto:
    Was observed as R$1.99 gross / R$0.00 net (discounted).
    NEVER USE — friction is high, reconciliation is slow, Pix is always preferred.

Platform fee policy:
  PF (CPF — 11 raw digits):
    - Outbound PIX (external key):  R$ 2.50 fixed   [Asaas R$2.00 + R$0.50 margin]
    - Inbound via charge/QR (new):  R$ 0.00          [free — competitive requirement]
    - Internal transfer:            R$ 0.00

  PJ (CNPJ — 14 raw digits):
    - Outbound PIX (external key):  max(R$3.00, 0.80% of value)
                                    [Asaas R$2.00 + R$1.00 min margin, scales above R$375]
    - Inbound via charge/QR (new):  max(R$0.49, 0.49% of value)
                                    [Asaas R$0.00 — pure platform revenue, zero cost;
                                     0.49% rate is below Asaas market reference of R$1.99]
    - Boleto pay:                   R$ 2.99 fixed    [kept for legacy reference only]
    - Internal transfer:            R$ 0.00

Scalability projection (1 000 external PJ transactions/month at avg R$500):
  Asaas outbound cost (after 100 free):  900 x R$2.00 = R$1,800
  Platform outbound revenue:             1000 x max(R$3.00, 0.80% x R$500)
                                       = 1000 x R$4.00 = R$4,000
  Net outbound margin:                   R$2,200

  Platform inbound revenue (1 000 charges at avg R$200):
  Asaas cost:                            R$0.00
  Platform revenue:                      1000 x max(R$0.49, 0.49% x R$200)
                                       = 1000 x R$0.98 = R$980
  Combined monthly net margin:           R$3,180+

Pix-only strategy — NEVER use boleto. Use only:
  1. Pix cobranca (charge created via API) — receive via any Pix channel
  2. Pix QR Code static  — receive via scan
  3. Pix QR Code dynamic (cobv/cob) — receive with expiry and metadata
  4. Pix copy-paste (copia e cola / EMV payload) — receive or send
  5. Pix transfer via key — outbound only, subject to outbound fee

Constants prefixed ASAAS_ document gateway costs at the time of measurement.
Update them whenever Asaas publishes pricing changes.
"""
from decimal import Decimal, ROUND_HALF_UP
import re


_TWO_PLACES = Decimal("0.01")

# ---------------------------------------------------------------- Asaas costs
# Verified from Asaas statement and dashboard on 11/03/2026.
# Inbound net cost is R$0.00 because Asaas currently offsets both the pix fee
# and messaging fee with "Desconto na tarifa" / "Desconto na taxa de mensageria".
# Monitor monthly: partial discount (R$0.32 instead of R$0.99) appeared at
# end of 11/03 cycle, indicating quota exhaustion near that volume level.
ASAAS_PIX_OUTBOUND_COST      = Decimal("2.00")  # per external PIX sent (after free quota)
ASAAS_PIX_INBOUND_GROSS_COST = Decimal("2.98")  # R$1.99 + R$0.99 gross (fully discounted back)
ASAAS_PIX_INBOUND_NET_COST   = Decimal("0.00")  # effective cost on current plan
ASAAS_BOLETO_COST            = Decimal("1.99")  # per boleto (do not use)
ASAAS_PIX_FREE_MONTHLY       = 100              # free outbound operations per month

# ---------------------------------------------------------------- PF constants
_PIX_SENT_PF = Decimal("2.50")  # ASAAS_PIX_OUTBOUND_COST (R$2.00) + R$0.50 margin
_PIX_RECV_PF = Decimal("0.00")  # Inbound free for PF — competitive requirement

# ---------------------------------------------------------------- PJ constants
# Outbound: percentage scales above R$375; below, flat R$3.00 guarantees R$1.00 margin.
_PIX_SENT_RATE_PJ = Decimal("0.0080")  # 0.80% of value
_PIX_SENT_MIN_PJ  = Decimal("3.00")   # min: Asaas R$2.00 + R$1.00 margin

# Inbound: Asaas net cost is R$0.00 — every cent charged is pure platform revenue.
# 0.49% rate stays well below Asaas reference fee of R$1.99.
_PIX_RECV_RATE_PJ = Decimal("0.0049")  # 0.49% of received value
_PIX_RECV_MIN_PJ  = Decimal("0.49")   # minimum per inbound charge

# Boleto: legacy reference only — never offered to new clients.
_BOLETO_PF = Decimal("2.49")
_BOLETO_PJ = Decimal("2.99")


def _raw_digits(cpf_cnpj) -> str:
    if not isinstance(cpf_cnpj, str):
        cpf_cnpj = str(cpf_cnpj) if cpf_cnpj is not None else ""
    return re.sub(r"\D", "", cpf_cnpj)


def is_pj(cpf_cnpj: str) -> bool:
    """Returns True when the document is a CNPJ (14 digits)."""
    return len(_raw_digits(cpf_cnpj)) == 14


def calculate_pix_outbound_fee(cpf_cnpj: str, amount: float) -> Decimal:
    """
    Fee charged to the platform client for an EXTERNAL outbound PIX transfer.

    Asaas underlying cost: R$2.00 (after free monthly quota).
    Margin guaranteed: R$0.50 (PF) / R$1.00+ (PJ).

    PF: R$2.50 fixed.
    PJ: max(R$3.00, 0.80% of amount).
        Break-even with Asaas cost at R$250; guaranteed R$1.00 margin at R$375+.

    Internal transfers → always call with is_external=False via calculate_pix_fee.
    """
    value = Decimal(str(amount))
    if is_pj(cpf_cnpj):
        fee = value * _PIX_SENT_RATE_PJ
        return max(fee, _PIX_SENT_MIN_PJ).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return _PIX_SENT_PF


def calculate_pix_receive_fee(cpf_cnpj: str, amount: float) -> Decimal:
    """
    Fee charged to the platform client (PJ only) for RECEIVING a PIX via
    cobranca, QR code (static or dynamic), copy-paste, or direct deposit.

    Asaas underlying cost: R$0.00 net (fully discounted on current plan).
    Every cent collected here is pure platform revenue with zero gateway liability.

    PF: R$0.00 — free, competitive requirement aligned with BCB Resolution 1/2020.
    PJ: max(R$0.49, 0.49% of received amount).
        Example: R$5 -> R$0.49; R$100 -> R$0.49; R$200 -> R$0.98; R$1,000 -> R$4.90.
        Rate (0.49%) stays below Asaas reference market fee of R$1.99 per transaction.
    """
    if not is_pj(cpf_cnpj):
        return Decimal("0.00")
    value = Decimal(str(amount))
    fee = value * _PIX_RECV_RATE_PJ
    return max(fee, _PIX_RECV_MIN_PJ).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def calculate_pix_fee(
    cpf_cnpj: str,
    amount: float,
    *,
    is_external: bool,
    is_received: bool = False,
) -> Decimal:
    """
    Unified PIX fee dispatcher — preserves backward-compatible call signature.

    Delegates to calculate_pix_outbound_fee or calculate_pix_receive_fee.
    Returns R$0.00 for internal transfers (is_external=False).

    Args:
        cpf_cnpj:    Raw CPF or CNPJ string of the account holder.
        amount:      Transaction value in BRL.
        is_external: True for external (inter-bank) transfers; False for internal.
        is_received: True when the transaction is incoming (charge paid by third party).
    """
    if not is_external:
        return Decimal("0.00")
    if is_received:
        return calculate_pix_receive_fee(cpf_cnpj, amount)
    return calculate_pix_outbound_fee(cpf_cnpj, amount)


def minimum_viable_outbound_amount(cpf_cnpj: str) -> Decimal:
    """
    Returns the minimum transaction amount at which the outbound Pix fee
    covers the Asaas gateway cost, guaranteeing positive platform margin.

    PF: R$1.00 — the flat fee R$2.50 always covers the R$2.00 Asaas cost
        regardless of amount. Minimum margin R$0.50 on every PF outbound.

    PJ: R$1.00 — the minimum platform fee is R$3.00 flat, which always
        exceeds the R$2.00 Asaas cost. Minimum guaranteed margin R$1.00.
        The 0.80% percentage overtakes the R$3.00 floor at R$375:
        - R$375: 0.80% x 375 = R$3.00 = floor (margin = R$1.00)
        - R$375+: fee = 0.80% x amount > R$3.00, margin > R$1.00 (scales)
        The "R$250 breakeven" referenced in the module docstring is the
        hypothetical break-even if NO minimum fee existed (0.80% x 250 = R$2.00),
        but is NOT the real billing model.

    Note: This function is informational. The service layer does not block
    transactions below this threshold — it informs via fee-preview. Blocking
    sub-threshold transactions requires a separate policy gate in the router.
    """
    return Decimal("1.00")


# Network fee exposed to users as "Taxa de Rede" (hides Asaas identity).
# For outbound: R$2.00 pass-through of Asaas cost (within free quota the amount
# becomes platform profit, captured via audit correction).
# For inbound: R$0.00 — Asaas fully discounts on current plan inside quota.
PLATFORM_PIX_OUTBOUND_NETWORK_FEE = ASAAS_PIX_OUTBOUND_COST   # R$2.00
PLATFORM_PIX_INBOUND_NETWORK_FEE  = ASAAS_PIX_INBOUND_NET_COST  # R$0.00


def calculate_pix_network_fee(cpf_cnpj: str, amount: float, *, is_external: bool, is_received: bool = False) -> Decimal:
    """Network fee pass-through shown to user as 'Taxa de Rede'."""
    if not is_external or is_received:
        return Decimal("0.00")
    return PLATFORM_PIX_OUTBOUND_NETWORK_FEE


def calculate_pix_service_fee(cpf_cnpj: str, amount: float, *, is_external: bool, is_received: bool = False) -> Decimal:
    """Pure platform margin: total_fee minus network pass-through."""
    total = calculate_pix_fee(cpf_cnpj, amount, is_external=is_external, is_received=is_received)
    net   = calculate_pix_network_fee(cpf_cnpj, amount, is_external=is_external, is_received=is_received)
    result = total - net
    return result.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def fee_breakdown(cpf_cnpj: str, amount: float, *, is_external: bool, is_received: bool = False) -> dict:
    """
    Returns a structured breakdown used by the fee-preview API endpoint.

    Fields:
      gateway_cost   : Asaas underlying cost for this operation.
      platform_fee   : Total fee charged to the platform client.
      network_fee    : Pass-through of Asaas cost, shown as 'Taxa de Rede' to users.
      service_fee    : Pure platform margin (platform_fee - network_fee).
      net_margin     : platform_fee - gateway_cost (platform gross profit).
      fee_label      : Human-readable label for UI display.
      fee_display    : Formatted string (e.g. "R$ 3,00").
      is_zero_cost   : True when Asaas charges nothing for this operation.
    """
    if not is_external:
        return {
            "gateway_cost": Decimal("0.00"),
            "platform_fee": Decimal("0.00"),
            "network_fee":  Decimal("0.00"),
            "service_fee":  Decimal("0.00"),
            "net_margin":   Decimal("0.00"),
            "fee_label":    "Transferência interna — gratuita",
            "fee_display":  "Gratuito",
            "is_zero_cost": True,
        }

    if is_received:
        gw_cost  = ASAAS_PIX_INBOUND_NET_COST
        p_fee    = calculate_pix_receive_fee(cpf_cnpj, amount)
        net_fee  = PLATFORM_PIX_INBOUND_NETWORK_FEE
        svc_fee  = p_fee
        label    = (
            "Taxa de recebimento PJ (0,49%, mín. R$ 0,49)"
            if is_pj(cpf_cnpj)
            else "Recebimento gratuito"
        )
    else:
        gw_cost  = ASAAS_PIX_OUTBOUND_COST
        p_fee    = calculate_pix_outbound_fee(cpf_cnpj, amount)
        net_fee  = PLATFORM_PIX_OUTBOUND_NETWORK_FEE
        svc_fee  = (p_fee - net_fee).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        label    = (
            "Taxa PJ (0,80% do valor, mín. R$ 3,00)"
            if is_pj(cpf_cnpj)
            else "Taxa de serviço (Pix externo)"
        )

    return {
        "gateway_cost": gw_cost,
        "platform_fee": p_fee,
        "network_fee":  net_fee,
        "service_fee":  svc_fee,
        "net_margin":   (p_fee - gw_cost).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP),
        "fee_label":    label,
        "fee_display":  fee_display(p_fee),
        "is_zero_cost": gw_cost == Decimal("0.00"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Monthly quota constants (verified from Asaas account statement 11/03/2026)
# ─────────────────────────────────────────────────────────────────────────────
# The first 30 outbound PIX transfers per month incur ZERO Asaas cost.
# Every subsequent transfer costs R$2.00 (confirmed: two R$2.00 charges on 11/03
# after ~30 outbound transactions were exhausted in the same month).
# The first 100 inbound PIX charges per month are also fully discounted (R$0.00 net).
# Partial mensageria discount (R$0.32 vs R$0.99) observed on the last inbound of
# 11/03 signals the messaging quota reaching near-exhaustion.
ASAAS_PIX_OUTBOUND_FREE_MONTHLY = 30   # free outbound PIX transfers per month
ASAAS_PIX_INBOUND_FREE_MONTHLY  = 100  # free inbound PIX charges per month


def monthly_revenue_projection(
    *,
    active_users: int,
    tx_per_user_per_month: float,
    pj_ratio: float = 0.20,
    avg_outbound_value: float = 150.0,
    avg_inbound_value: float = 100.0,
    inbound_tx_ratio: float = 0.50,
    outbound_free_quota_used: int = 0,
) -> dict:
    """
    Computes a monthly revenue projection for the platform.

    Revenue model:
      PF outbound  : R$2.50 fixed per transaction
      PJ outbound  : max(R$3.00, 0.80% x avg_value) per transaction
      PJ inbound   : max(R$0.49, 0.49% x avg_value) per transaction — pure revenue (no Asaas cost)
      PF inbound   : R$0.00 — free by design

    Cost model:
      Outbound Asaas: R$0.00 for first (30 - outbound_free_quota_used) transfers,
                      R$2.00 flat for all subsequent.
      Inbound Asaas:  R$0.00 (within free quota).

    Args:
        active_users             : Total active users on the platform.
        tx_per_user_per_month    : Average outbound transactions per user per month.
        pj_ratio                 : Fraction of PJ accounts (0.0 – 1.0). Default 0.20.
        avg_outbound_value       : Average value of an outbound PIX (BRL). Default R$150.
        avg_inbound_value        : Average value of an inbound PIX charge (BRL). Default R$100.
        inbound_tx_ratio         : Fraction of users who also receive. Default 0.50.
        outbound_free_quota_used : Free outbound slots already consumed this month.
    """
    pf_ratio = 1.0 - pj_ratio

    pf_out_count = active_users * pf_ratio * tx_per_user_per_month
    pj_out_count = active_users * pj_ratio * tx_per_user_per_month
    total_out_count = pf_out_count + pj_out_count
    pj_in_count = active_users * pj_ratio * inbound_tx_ratio * tx_per_user_per_month

    remaining_free = max(0, ASAAS_PIX_OUTBOUND_FREE_MONTHLY - outbound_free_quota_used)
    free_used = min(remaining_free, total_out_count)
    paid_out_count = max(0.0, total_out_count - free_used)

    pf_out_fee_each = float(_PIX_SENT_PF)
    pj_out_fee_each = max(float(_PIX_SENT_MIN_PJ), float(_PIX_SENT_RATE_PJ) * avg_outbound_value)
    pj_in_fee_each  = max(float(_PIX_RECV_MIN_PJ), float(_PIX_RECV_RATE_PJ) * avg_inbound_value)

    pf_out_revenue  = pf_out_count * pf_out_fee_each
    pj_out_revenue  = pj_out_count * pj_out_fee_each
    outbound_revenue = pf_out_revenue + pj_out_revenue
    inbound_revenue  = pj_in_count * pj_in_fee_each

    asaas_outbound_cost = paid_out_count * float(ASAAS_PIX_OUTBOUND_COST)
    gross_revenue = outbound_revenue + inbound_revenue
    net_profit    = gross_revenue - asaas_outbound_cost
    margin_pct    = (net_profit / gross_revenue * 100.0) if gross_revenue > 0 else 0.0

    pf_margin_per_tx = pf_out_fee_each - float(ASAAS_PIX_OUTBOUND_COST)
    pj_margin_per_tx = pj_out_fee_each - float(ASAAS_PIX_OUTBOUND_COST)

    return {
        "inputs": {
            "active_users": active_users,
            "tx_per_user_per_month": tx_per_user_per_month,
            "pj_ratio_pct": round(pj_ratio * 100, 1),
            "avg_outbound_value": avg_outbound_value,
            "avg_inbound_value": avg_inbound_value,
        },
        "transactions": {
            "pf_outbound": round(pf_out_count),
            "pj_outbound": round(pj_out_count),
            "total_outbound": round(total_out_count),
            "pj_inbound": round(pj_in_count),
            "free_outbound_used": round(free_used),
            "paid_outbound": round(paid_out_count),
        },
        "revenue": {
            "pf_outbound": round(pf_out_revenue, 2),
            "pj_outbound": round(pj_out_revenue, 2),
            "outbound_total": round(outbound_revenue, 2),
            "pj_inbound": round(inbound_revenue, 2),
            "gross": round(gross_revenue, 2),
        },
        "costs": {
            "asaas_outbound": round(asaas_outbound_cost, 2),
            "asaas_inbound": 0.0,
            "total": round(asaas_outbound_cost, 2),
        },
        "profit": {
            "net": round(net_profit, 2),
            "margin_pct": round(margin_pct, 1),
        },
        "unit_economics": {
            "pf_outbound_fee": round(pf_out_fee_each, 2),
            "pj_outbound_fee": round(pj_out_fee_each, 2),
            "pj_inbound_fee": round(pj_in_fee_each, 2),
            "asaas_cost_per_paid_tx": float(ASAAS_PIX_OUTBOUND_COST),
            "pf_margin_per_tx": round(pf_margin_per_tx, 2),
            "pj_margin_per_tx": round(pj_margin_per_tx, 2),
            "first_30_margin_pct": 100.0,
        },
    }


def growth_projection(
    *,
    months: int = 12,
    initial_users: int = 10,
    monthly_user_growth_rate: float = 0.25,
    tx_per_user_per_month: float = 3.0,
    pj_ratio: float = 0.20,
    avg_outbound_value: float = 150.0,
    avg_inbound_value: float = 100.0,
) -> list:
    """
    Compound growth projection over N months.

    Users grow at `monthly_user_growth_rate` per month (compounding):
        users(n) = initial_users x (1 + monthly_user_growth_rate)^(n-1)

    Returns a list of monthly snapshots. Each snapshot records users, revenue,
    cost, net profit, and cumulative Matrix balance (sum of all prior profits).
    The cumulative Matrix balance is the platform's retained earns available
    for operational withdrawal or reinvestment.
    """
    snapshots = []
    cumulative_matrix = 0.0
    for month in range(1, months + 1):
        users_this_month = max(1, int(initial_users * ((1.0 + monthly_user_growth_rate) ** (month - 1))))
        p = monthly_revenue_projection(
            active_users=users_this_month,
            tx_per_user_per_month=tx_per_user_per_month,
            pj_ratio=pj_ratio,
            avg_outbound_value=avg_outbound_value,
            avg_inbound_value=avg_inbound_value,
        )
        net = p["profit"]["net"]
        cumulative_matrix = round(cumulative_matrix + net, 2)
        snapshots.append({
            "month": month,
            "users": users_this_month,
            "gross_revenue": p["revenue"]["gross"],
            "total_cost": p["costs"]["total"],
            "net_profit": net,
            "cumulative_matrix": cumulative_matrix,
            "margin_pct": p["profit"]["margin_pct"],
            "total_outbound_tx": p["transactions"]["total_outbound"],
        })
    return snapshots


def calculate_boleto_fee(cpf_cnpj: str) -> Decimal:
    """
    Returns the fixed fee for boleto payments based on account type.
    Boleto is deprecated — use Pix cobranca instead. Never offer boleto to
    new clients; this function is retained for legacy transaction history only.
    """
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
