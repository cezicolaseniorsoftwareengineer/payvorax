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
# Verified from Asaas statement 03/03/2026 through 15/03/2026.
#
# INBOUND cost history:
#   03/03 – 10/03: net R$0.00 — Asaas fully discounted both
#                  "Taxa do Pix" (R$1.99) and "Taxa de mensageria" (R$0.99)
#                  via "Desconto na tarifa" and "Desconto na taxa de mensageria".
#   11/03 (last):   partial mensageria refund (R$0.32, not R$0.99) signalled quota
#                  exhaustion.  Full billings on same day: R$2.98 net.
#   11/03 onwards: discounts stopped — effective cost is R$1.99/charge ("Taxa do Pix"
#                  only) or up to R$2.98 when mensageria is also billed.
#                  Conservative reference value adopted: R$1.99 (minimum observed).
#
# OUTBOUND cost history:
#   Monthly quota of 30 free transfers exhausted on 11/03.
#   Every outbound PIX after that: R$2.00 flat ("Taxa para Pix com chave").
#   Very small charges (< R$1.00 approx) were observed with no Asaas fee.
#
# Update ASAAS_PIX_INBOUND_NET_COST if Asaas resets quota or changes plan.
ASAAS_PIX_OUTBOUND_COST      = Decimal("2.00")  # per external PIX sent (after free quota)
ASAAS_PIX_INBOUND_GROSS_COST = Decimal("2.98")  # R$1.99 (taxa pix) + R$0.99 (mensageria) — peak cost
ASAAS_PIX_INBOUND_NET_COST   = Decimal("1.99")  # conservative effective cost: quota exhausted 11/03/2026
ASAAS_BOLETO_COST            = Decimal("1.99")  # per boleto (do not use)
ASAAS_PIX_FREE_MONTHLY       = 100              # legacy constant — use split constants below

# ---------------------------------------------------------------- Fee structure (15/03/2026)
# Every external outbound PIX charges the user R$4.00:
#   R$3.00 "Taxa de rede"        — pass-through of Asaas R$2.00 + R$1.00 surplus that
#                                   accumulates in Asaas and is swept to Matrix nightly.
#   R$1.00 "Taxa de manutencao" — credited to Matrix immediately on every transaction.
# The nightly audit sweep (00:00 BRT) transfers any Asaas surplus to Matrix.
PIX_NETWORK_FEE         = Decimal("3.00")  # "Taxa de rede" — outbound component
PIX_MAINTENANCE_FEE     = Decimal("1.00")  # "Taxa de manutencao" — applies to all external operations
# Inbound rede component: R$2.00 - covers Asaas inbound cost + R$0.01 surplus.
# Changed 18/03/2026: user requirement established R$2 rede + R$1 manutencao = R$3 flat for inbound.
PIX_INBOUND_NETWORK_FEE = Decimal("2.00")  # "Taxa de rede" — inbound component (18/03/2026)

# ---------------------------------------------------------------- PF constants
# Outbound: R$3.00 rede + R$1.00 manutencao = R$4.00 total.
# Reinstated 18/03/2026 after cost incident: Asaas charges R$2.00/transfer;
# platform must collect R$4.00 to cover gateway cost and maintain positive margin.
_PIX_SENT_PF = PIX_NETWORK_FEE + PIX_MAINTENANCE_FEE             # R$4.00 = R$3 rede + R$1 manutencao
_PIX_RECV_PF = Decimal("4.00")  # Deposits: R$4.00 flat fee — same as outbound

# ---------------------------------------------------------------- PJ constants
# Outbound: minimum R$4.00; percentage 0.80% applies above R$500 (0.80% x 500 = R$4.00 breakeven).
# Reinstated 18/03/2026 — zero-fee policy removed after cost incident.
_PIX_SENT_RATE_PJ = Decimal("0.0080")  # 0.80% of value for PJ clients
_PIX_SENT_MIN_PJ  = PIX_NETWORK_FEE + PIX_MAINTENANCE_FEE        # R$4.00 minimum

# Inbound: Asaas cost is R$1.99/charge (quota exhausted 11/03/2026).
# Floor R$2.00 ensures R$0.01 margin at every charge value.
_PIX_RECV_RATE_PJ = Decimal("0.0049")  # 0.49% of received value
_PIX_RECV_MIN_PJ  = Decimal("2.00")   # minimum: covers Asaas R$1.99 + R$0.01 margin

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

    Fee structure (15/03/2026):
      PF: R$4.00 fixed = R$3.00 taxa de rede + R$1.00 taxa de manutencao.
          R$1.00 manutencao is credited to Matrix immediately.
          R$1.00 surplus (from R$3.00 rede minus R$2.00 Asaas cost) accumulates
          in Asaas and is swept to Matrix by the nightly audit at 00:00 BRT.
      PJ: max(R$4.00, 0.80% of amount).
          Minimum mirrors PF: R$2.00 Asaas + R$1.00 surplus + R$1.00 manutencao.

    Internal transfers → always call with is_external=False via calculate_pix_fee.
    """
    return _PIX_SENT_PF  # R$4.00 flat — PF and PJ, any value


def calculate_pix_receive_fee(cpf_cnpj: str, amount: float) -> Decimal:
    """
    Fee charged for RECEIVING a PIX from another bank (bank-to-bank deposit).

    Policy: R$4.00 flat for all users (PF and PJ), any value.
    Same flat rate as outbound — no PF/PJ distinction, no percentage.
    """
    return _PIX_RECV_PF  # R$4.00 flat


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
    Internal transfers (is_external=False) are free: returns Decimal("0.00").

    Args:
        cpf_cnpj:    Raw CPF or CNPJ string of the account holder.
        amount:      Transaction value in BRL.
        is_external: True for external (inter-bank) transfers; False for internal.
        is_received: True when the transaction is incoming (charge paid by third party).
    """
    if not is_external:
        return Decimal("0.00")  # Internal transfers are free
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
# For outbound: R$3.00 charged to user (R$2.00 Asaas pass-through + R$1.00 surplus
#              swept nightly to Matrix by balance audit).
# For inbound: R$2.00 (covers Asaas R$1.99 + R$0.01 margin).
PLATFORM_PIX_OUTBOUND_NETWORK_FEE = PIX_NETWORK_FEE              # R$3.00
PLATFORM_PIX_INBOUND_NETWORK_FEE  = PIX_INBOUND_NETWORK_FEE      # R$1.00 — rede component only


def calculate_pix_network_fee(cpf_cnpj: str, amount: float, *, is_external: bool, is_received: bool = False) -> Decimal:
    """Network fee pass-through shown to user as 'Taxa de Rede'."""
    if not is_external:
        return Decimal("0.00")        # internal: completely free — no fees of any kind
    if is_received:
        return PIX_INBOUND_NETWORK_FEE  # R$1.00 — Asaas R$1.99 rounded down, pass-through component
    return PLATFORM_PIX_OUTBOUND_NETWORK_FEE  # R$3.00


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
            "fee_label":    "Transferencia interna gratuita",
            "fee_display":  fee_display(Decimal("0.00")),
            "is_zero_cost": True,
        }

    if is_received:
        gw_cost  = ASAAS_PIX_INBOUND_NET_COST
        p_fee    = calculate_pix_receive_fee(cpf_cnpj, amount)
        net_fee  = PIX_INBOUND_NETWORK_FEE
        svc_fee  = (p_fee - net_fee).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        label    = "Taxa de servico: R$ 4,00"
    else:
        gw_cost  = ASAAS_PIX_OUTBOUND_COST
        p_fee    = calculate_pix_outbound_fee(cpf_cnpj, amount)
        net_fee  = PLATFORM_PIX_OUTBOUND_NETWORK_FEE
        svc_fee  = (p_fee - net_fee).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        label    = "Taxa de servico: R$ 4,00"

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
      PJ inbound   : max(R$0.49, 0.49% x avg_value) per transaction
      PF inbound   : R$0.00 — free by policy (structural subsidy)

    Cost model (post-quota — 11/03/2026 onwards):
      Outbound Asaas: R$0.00 for first (30 - outbound_free_quota_used) transfers,
                      R$2.00 flat for each subsequent transfer.
      Inbound Asaas:  R$1.99 per charge (quota exhausted; discounts ended 11/03/2026).
                      PF inbound is a structural net loss: R$0.00 revenue, R$1.99 cost.
                      PJ inbound break-even: R$1.99 / 0.0049 = ~R$406 per charge.

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
    # Every inbound charge now costs R$1.99 (quota exhausted 11/03/2026).
    # PF inbound count approximated as (total_users * pf_ratio * inbound_tx_ratio * tx_rate).
    pf_in_count = active_users * pf_ratio * inbound_tx_ratio * tx_per_user_per_month
    total_in_count = pf_in_count + pj_in_count
    asaas_inbound_cost = total_in_count * float(ASAAS_PIX_INBOUND_NET_COST)
    gross_revenue = outbound_revenue + inbound_revenue
    net_profit    = gross_revenue - asaas_outbound_cost - asaas_inbound_cost
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
            "pf_inbound": round(pf_in_count),
            "pj_inbound": round(pj_in_count),
            "total_inbound": round(total_in_count),
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
            "asaas_inbound": round(asaas_inbound_cost, 2),
            "total": round(asaas_outbound_cost + asaas_inbound_cost, 2),
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
    Returns the fixed fee for boleto payments.
    Boleto is deprecated — use Pix cobranca instead. Never offer boleto to
    new clients; this function is retained for legacy transaction history only.
    Fee: R$4.00 flat, same as all other external operations.
    """
    return Decimal("4.00")


def fee_display(fee: Decimal) -> str:
    """
    Formats a fee amount as a human-readable Brazilian Real string.
    Returns 'Gratuito' when zero.
    """
    if fee == Decimal("0.00"):
        return "Gratuito"
    formatted = f"{float(fee):.2f}".replace(".", ",")
    return f"R$ {formatted}"
