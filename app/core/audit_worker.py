"""
Balance Audit Worker — BioCodeTechPay.

Perpetual background task (60-second cycle) that:
  1. Compares the sum of all internal user balances (customers + Matrix) against
     the Asaas account real balance.
  2. Auto-corrects detected divergences within safety limits (max R$20).
  3. Calls OpenRouter AI (gpt-4o-mini) to produce a structured financial analysis
     of each divergence/correction for operator visibility and audit trails.
  4. Runs a comprehensive end-of-day reconciliation once per calendar day
     (UTC midnight) to ensure every balance is precise to 2 decimal places and
     the internal total perfectly matches Asaas before the next business day.

Invariants:
  - Auto-correction cap: R$20 per cycle.  Larger diffs are flagged for manual review.
  - Matrix balance never goes negative.
  - All arithmetic uses Decimal; float is only used at DB boundary.
  - OpenRouter failure is non-blocking: audit continues without AI analysis.
"""
import asyncio
from datetime import date, timezone, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import httpx

from app.core.logger import audit_log, logger

_AUDIT_INTERVAL_SECONDS = 60   # 1 minute — perpetual cycle
_AUTO_CORRECTION_MAX    = Decimal("20.00")
_TWO_PLACES             = Decimal("0.01")


async def _call_openrouter_analysis(
    internal_sum: float,
    matrix_balance: float,
    total_internal: float,
    asaas_balance: Optional[float],
    signed_diff: Optional[float],
    status: str,
    correction_applied: Optional[dict],
    customers: int,
) -> str:
    """
    Calls OpenRouter (gpt-4o-mini) to produce a concise financial analysis of
    the current audit cycle result.  Non-blocking: returns empty string on failure.

    The AI has full context of the invariants and is asked to:
      - Explain the probable cause of any divergence.
      - Confirm whether the auto-correction taken was appropriate.
      - Flag any additional action the operator should take.
    """
    from app.core.config import settings

    if not settings.OPENROUTER_API_KEY:
        return ""

    abs_diff = abs(signed_diff) if signed_diff is not None else 0.0
    direction = (
        "internal > Asaas (correntista creditado a maior)"
        if signed_diff and signed_diff > 0
        else "Asaas > internal (saldo gateway nao refletido internamente)"
        if signed_diff and signed_diff < 0
        else "sem divergencia"
    )

    if correction_applied:
        action = correction_applied.get("action", "desconhecido")
        amount = correction_applied.get("amount") or correction_applied.get("matrix_debited", 0.0)
        corr_desc = f"sim — acao '{action}', valor R${amount:.2f}"
    else:
        corr_desc = "nao"

    prompt = (
        "Voce e o sistema de inteligencia financeira autonoma do BioCodeTechPay (fintech brasileira).\n"
        "Acabou de concluir um ciclo de auditoria de saldos perpetua (intervalo 60 segundos).\n\n"
        "Dados do ciclo atual:\n"
        f"  - Saldo clientes (soma): R$ {internal_sum:.2f}\n"
        f"  - Saldo Conta Matrix (acumulo de taxas da plataforma): R$ {matrix_balance:.2f}\n"
        f"  - Total interno (clientes + Matrix): R$ {total_internal:.2f}\n"
        f"  - Saldo Asaas (conta real do gateway): {f'R$ {asaas_balance:.2f}' if asaas_balance is not None else 'indisponivel'}\n"
        f"  - Diferenca absoluta: R$ {abs_diff:.2f}\n"
        f"  - Direcao: {direction}\n"
        f"  - Status auditoria: {status}\n"
        f"  - Correcao automatica aplicada: {corr_desc}\n"
        f"  - Numero de correntistas: {customers}\n\n"
        "Regras de negocio invariantes:\n"
        "  1. Conta Matrix acumula APENAS a margem da plataforma (taxa cobrada ao correntista menos "
        "custo Asaas). Nunca deve ir negativa.\n"
        "  2. Quando total interno > Asaas: Asaas cobrou taxa de gateway nao refletida internamente. "
        "A correcao correta e debitar da Conta Matrix (plataforma absorve o custo como reducao de margem). "
        "NUNCA modificar saldo de correntistas — esse e um invariante absoluto do sistema.\n"
        "  3. Quando Asaas > total interno: existe um credito no Asaas nao registrado internamente. "
        "Creditar a Matrix ate igualar.\n"
        "  4. O sistema auto-corrige divergencias ate R$20,00 por ciclo.\n\n"
        "Com base nesses dados, responda EM PORTUGUES BRASILEIRO em ate 3 frases curtas:\n"
        "  a) Qual a causa mais provavel da divergencia (se houver)?\n"
        "  b) A correcao aplicada foi a correta?\n"
        "  c) Ha alguma acao adicional necessaria pelo operador?\n"
        "Se status for OK, confirme brevemente que tudo esta correto e nenhuma acao e necessaria."
    )

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://new-credit-fintech.onrender.com",
                    "X-Title": "BioCodeTechPay AutonomousAudit",
                },
                json={
                    "model": "openai/gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 350,
                    "temperature": 0.2,
                },
            )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        logger.warning(f"[audit-worker/ai] OpenRouter returned HTTP {resp.status_code}")
    except Exception as ai_err:
        logger.warning(f"[audit-worker/ai] OpenRouter call failed: {ai_err}")
    return ""


async def _run_single_audit(db_factory, gateway_factory) -> dict:
    """
    Executes one balance audit cycle with auto-correction.

    Uses Decimal arithmetic throughout to avoid IEEE 754 float drift (e.g.
    sum of many floats accumulating rounding errors across user balances).

    When internal total exceeds Asaas balance (diff > 0), the divergence is caused
    by gateway fees that reduced the Asaas account but were not reflected internally.
    Correction: debit Matrix only (platform absorbs the Asaas gateway cost as a
    reduction in margin). Correntista balances are NEVER modified by the audit system.

    When Asaas exceeds internal (diff < 0), an unregistered credit exists in
    the gateway. Correction: credit the Matrix account to restore parity.

    Returns a dict with status, totals, diff, correction metadata and ai_analysis.
    Never raises — all exceptions are caught and reported.
    """
    from app.auth.models import User
    from app.core.config import settings

    try:
        db = db_factory()
        try:
            all_users = db.query(User).all()
            matrix_user = next((u for u in all_users if u.email == settings.MATRIX_ACCOUNT_EMAIL), None)
            customers = [u for u in all_users if u.email != settings.MATRIX_ACCOUNT_EMAIL]

            # Use Decimal to accumulate balances without float drift
            internal_sum_dec  = sum(
                (Decimal(str(u.balance)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP) for u in customers),
                Decimal("0.00"),
            )
            matrix_bal_dec    = (
                Decimal(str(matrix_user.balance)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
                if matrix_user else Decimal("0.00")
            )
            total_internal_dec = internal_sum_dec + matrix_bal_dec
        finally:
            db.close()

        internal_sum   = float(internal_sum_dec)
        matrix_balance = float(matrix_bal_dec)
        total_internal = float(total_internal_dec)

        asaas_balance: Optional[float] = None
        gateway = gateway_factory()
        if gateway and hasattr(gateway, "_make_request"):
            try:
                resp = gateway._make_request("GET", "/finance/balance")
                asaas_balance = float(resp.get("balance", 0))
            except Exception as gw_err:
                logger.warning(f"[audit-worker] Asaas balance fetch failed: {gw_err}")

        asaas_dec     = Decimal(str(asaas_balance)).quantize(_TWO_PLACES, ROUND_HALF_UP) if asaas_balance is not None else None
        signed_diff   = float((total_internal_dec - asaas_dec).quantize(_TWO_PLACES, ROUND_HALF_UP)) if asaas_dec is not None else None
        abs_diff_dec  = abs(Decimal(str(signed_diff)).quantize(_TWO_PLACES, ROUND_HALF_UP)) if signed_diff is not None else None
        abs_diff      = float(abs_diff_dec) if abs_diff_dec is not None else None

        result: dict = {
            "customers": len(customers),
            "internal_sum": internal_sum,
            "matrix_balance": matrix_balance,
            "total_internal": total_internal,
            "asaas_balance": float(asaas_dec) if asaas_dec is not None else None,
            "diff": abs_diff,
            "signed_diff": signed_diff,
            "status": "OK",
            "correction_applied": None,
            "ai_analysis": None,
        }

        if asaas_balance is None:
            result["status"] = "WARN_NO_GATEWAY"
            logger.info(
                "[audit-worker] Partial audit (no Asaas). "
                f"internal_total=R${total_internal:.2f} matrix=R${matrix_balance:.2f} customers={len(customers)}"
            )
        elif abs_diff_dec < Decimal("0.01"):
            result["status"] = "OK"
            logger.info(
                f"[audit-worker] PASS — internal=R${total_internal:.2f} asaas=R${asaas_balance:.2f} diff=R${abs_diff:.2f}"
            )
        else:
            # Divergence detected
            if Decimal(str(signed_diff)) > Decimal("0.01") and abs_diff_dec <= _AUTO_CORRECTION_MAX and matrix_user is not None:
                # Internal exceeds Asaas: Asaas deducted a gateway fee not reflected internally.
                # INVARIANT: platform (Matrix) absorbs the Asaas gateway cost as a margin reduction.
                # Correntista balances are NEVER modified by the audit system under any circumstance.
                db2 = db_factory()
                try:
                    from app.auth.models import User as _User
                    live_matrix = db2.query(_User).filter(_User.email == settings.MATRIX_ACCOUNT_EMAIL).first()

                    if live_matrix is not None:
                        matrix_cur_dec = Decimal(str(live_matrix.balance)).quantize(_TWO_PLACES, ROUND_HALF_UP)
                        matrix_debit   = min(abs_diff_dec, max(Decimal("0.00"), matrix_cur_dec))
                        live_matrix.balance = float(
                            (matrix_cur_dec - matrix_debit).quantize(_TWO_PLACES, ROUND_HALF_UP)
                        )
                        db2.commit()
                        _remainder_unfunded = float(
                            (abs_diff_dec - matrix_debit).quantize(_TWO_PLACES, ROUND_HALF_UP)
                        )
                        result["correction_applied"] = {
                            "action": "matrix_debited",
                            "matrix_debited": float(matrix_debit),
                            "matrix_balance_after": float(live_matrix.balance),
                            "correntistas_unchanged": True,
                            "remainder_unfunded": _remainder_unfunded,
                        }
                        audit_log(
                            action="AUDIT_AUTO_CORRECTION",
                            user="audit-worker",
                            resource="matrix_balance",
                            details={
                                "diff": abs_diff,
                                "direction": "internal_above_asaas",
                                "matrix_debited": float(matrix_debit),
                                "matrix_balance_after": float(live_matrix.balance),
                                "correntistas_unchanged": True,
                                "asaas_balance": asaas_balance,
                            },
                        )
                        if _remainder_unfunded > 0.01:
                            logger.warning(
                                f"[audit-worker] Partial correction: R${_remainder_unfunded:.2f} unfunded — "
                                "Matrix balance insufficient to absorb full divergence. Manual action required."
                            )
                        logger.info(
                            f"[audit-worker] AUTO-CORRECTION: Matrix debited R${float(matrix_debit):.2f} "
                            f"(new_matrix=R${live_matrix.balance:.2f}). Correntistas unchanged (invariant)."
                        )
                        result["status"] = "AUTO_CORRECTED"
                    else:
                        result["status"] = "ERROR"
                        logger.error("[audit-worker] Cannot auto-correct: Matrix account not found")
                finally:
                    db2.close()

            elif Decimal(str(signed_diff)) < Decimal("-0.01") and abs_diff_dec <= _AUTO_CORRECTION_MAX and matrix_user is not None:
                # Asaas exceeds internal: unregistered credit in gateway. Credit Matrix.
                db2 = db_factory()
                try:
                    from app.auth.models import User as _User
                    live_matrix = db2.query(_User).filter(_User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
                    if live_matrix is not None:
                        old_dec = Decimal(str(live_matrix.balance)).quantize(_TWO_PLACES, ROUND_HALF_UP)
                        live_matrix.balance = float((old_dec + abs_diff_dec).quantize(_TWO_PLACES, ROUND_HALF_UP))
                        db2.commit()
                        result["correction_applied"] = {
                            "action": "matrix_credited",
                            "amount": abs_diff,
                            "matrix_balance_after": float(live_matrix.balance),
                        }
                        audit_log(
                            action="AUDIT_AUTO_CORRECTION",
                            user="audit-worker",
                            resource="matrix_balance",
                            details={
                                "diff": abs_diff,
                                "direction": "asaas_above_internal",
                                "matrix_balance_after": float(live_matrix.balance),
                                "asaas_balance": asaas_balance,
                            },
                        )
                        logger.info(
                            f"[audit-worker] AUTO-CORRECTION applied: Matrix credited R${abs_diff:.2f} "
                            f"new_matrix=R${live_matrix.balance:.2f}"
                        )
                        result["status"] = "AUTO_CORRECTED"
                    else:
                        result["status"] = "WARN"
                        logger.warning("[audit-worker] Cannot auto-correct: Matrix account not found")
                finally:
                    db2.close()
            else:
                # diff > R$20 on either side — structural imbalance, manual action required
                result["status"] = "ERROR" if abs_diff_dec >= Decimal("10.00") else "WARN"
                logger.warning(
                    f"[audit-worker] DIVERGENCE (no-autocorrect) "
                    f"direction={'internal_above_asaas' if signed_diff > 0 else 'asaas_above_internal'} "
                    f"diff=R${abs_diff:.2f} internal=R${total_internal:.2f} asaas=R${asaas_balance:.2f}"
                )

        # AI analysis: call OpenRouter for every non-OK cycle or when a correction was applied
        if result["status"] != "OK":
            ai_text = await _call_openrouter_analysis(
                internal_sum=internal_sum,
                matrix_balance=matrix_balance,
                total_internal=total_internal,
                asaas_balance=asaas_balance,
                signed_diff=signed_diff,
                status=result["status"],
                correction_applied=result["correction_applied"],
                customers=len(customers),
            )
            if ai_text:
                result["ai_analysis"] = ai_text
                audit_log(
                    action="AUDIT_AI_ANALYSIS",
                    user="audit-worker",
                    resource="balance_reconciliation",
                    details={"analysis": ai_text, "status": result["status"]},
                )

        return result

    except Exception as exc:
        logger.error(f"[audit-worker] Unexpected error in audit cycle: {exc}")
        return {"status": "EXCEPTION", "error": str(exc)}


async def _run_end_of_day_reconciliation(db_factory, gateway_factory) -> dict:
    """
    Comprehensive end-of-day balance reconciliation.

    Runs once per calendar day (UTC).  Steps:
      1. Round all user balances to exactly 2 decimal places (fix accumulated
         float drift from many += operations against the SQLite/PostgreSQL float column).
      2. Run a full audit cycle to detect and correct any residual divergence
         against Asaas.
      3. Log the final state with AI analysis via OpenRouter.

    Returns the audit result dict augmented with eod=True.
    """
    from app.auth.models import User as _User
    from app.core.config import settings

    logger.info("[audit-worker/eod] Starting end-of-day reconciliation")

    # Step 1: round all balances to 2 decimal places to eliminate float drift
    try:
        db = db_factory()
        try:
            users = db.query(_User).all()
            adjusted = 0
            for u in users:
                rounded = float(Decimal(str(u.balance)).quantize(_TWO_PLACES, ROUND_HALF_UP))
                if abs(rounded - float(u.balance)) > 1e-9:
                    u.balance = rounded
                    adjusted += 1
            if adjusted:
                db.commit()
                logger.info(f"[audit-worker/eod] Rounded {adjusted} user balance(s) to 2 decimal places")
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"[audit-worker/eod] Balance rounding failed: {exc}")

    # Step 2: full audit cycle (auto-corrects residual divergence)
    result = await _run_single_audit(db_factory, gateway_factory)
    result["eod"] = True

    # Step 3: dedicated EOD AI analysis regardless of status
    from app.core.config import settings as _settings
    if _settings.OPENROUTER_API_KEY:
        _eod_asaas = (
            f"R$ {result['asaas_balance']:.2f}"
            if result["asaas_balance"] is not None
            else "indisponivel"
        )
        eod_prompt = (
            "Voce e o sistema de inteligencia financeira autonoma do BioCodeTechPay.\n"
            "E o FIM DO DIA. Acabou de ser executada a reconciliacao completa de saldos.\n\n"
            f"Resultado final:\n"
            f"  - Status: {result['status']}\n"
            f"  - Saldo clientes: R$ {result['internal_sum']:.2f}\n"
            f"  - Saldo Matrix: R$ {result['matrix_balance']:.2f}\n"
            f"  - Total interno: R$ {result['total_internal']:.2f}\n"
            f"  - Saldo Asaas: {_eod_asaas}\n"
            f"  - Diferenca residual: R$ {(result['diff'] or 0):.2f}\n"
            f"  - Correcao final aplicada: {'sim' if result['correction_applied'] else 'nao'}\n\n"
            "Em 2 frases, confirme se o dia foi encerrado com saldos corretos, ou indique o que "
            "permanece pendente para acao manual no proximo dia util. Seja objetivo."
        )
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {_settings.OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://new-credit-fintech.onrender.com",
                        "X-Title": "BioCodeTechPay EODReconciliation",
                    },
                    json={
                        "model": "openai/gpt-4o-mini",
                        "messages": [{"role": "user", "content": eod_prompt}],
                        "max_tokens": 200,
                        "temperature": 0.1,
                    },
                )
            if resp.status_code == 200:
                eod_analysis = resp.json()["choices"][0]["message"]["content"].strip()
                result["eod_ai_analysis"] = eod_analysis
                audit_log(
                    action="AUDIT_EOD_AI_ANALYSIS",
                    user="audit-worker",
                    resource="end_of_day_reconciliation",
                    details={"analysis": eod_analysis, "status": result["status"]},
                )
                logger.info(f"[audit-worker/eod] AI summary: {eod_analysis}")
        except Exception as ai_err:
            logger.warning(f"[audit-worker/eod] OpenRouter EOD call failed: {ai_err}")

    logger.info(
        f"[audit-worker/eod] Reconciliation complete — status={result['status']} "
        f"internal=R${result['total_internal']:.2f} "
        f"asaas=R${(result['asaas_balance'] or 0):.2f}"
    )
    return result


async def balance_audit_loop(db_factory, gateway_factory, interval: int = _AUDIT_INTERVAL_SECONDS):
    """
    Perpetual async loop — runs indefinitely at fixed intervals.

    Every `interval` seconds (default 60):
      - Executes a full balance audit cycle with auto-correction.
      - Calls OpenRouter AI to analyse any divergence or correction found.

    Once per calendar day (UTC midnight detection):
      - Runs end-of-day comprehensive reconciliation that rounds all balances,
        corrects residual divergences and logs a final AI summary.

    Args:
        db_factory:      Callable returning a SQLAlchemy Session.
        gateway_factory: Callable returning a PaymentGatewayPort instance or None.
        interval:        Sleep duration in seconds between regular cycles.
    """
    logger.info(f"[audit-worker] Started. Interval: {interval}s. EOD reconciliation: active (UTC midnight).")

    # Let app fully boot before touching the database
    await asyncio.sleep(15)

    last_eod_date: Optional[date] = None

    while True:
        now_utc = datetime.now(tz=timezone.utc)
        today   = now_utc.date()

        # End-of-day reconciliation: fire once per day on the first cycle whose
        # wall-clock time is past 23:55 UTC and the date has not yet been reconciled.
        if now_utc.hour == 23 and now_utc.minute >= 55 and last_eod_date != today:
            try:
                await _run_end_of_day_reconciliation(db_factory, gateway_factory)
                last_eod_date = today
            except Exception as eod_exc:
                logger.error(f"[audit-worker/eod] Unhandled error: {eod_exc}")
        else:
            await _run_single_audit(db_factory, gateway_factory)

        await asyncio.sleep(interval)
