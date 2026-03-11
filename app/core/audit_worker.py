"""
Balance Audit Worker.

Periodic background task that compares the sum of all internal user balances
against the Asaas account balance to detect discrepancies early.

Run cycle: every 60 seconds (configurable via AUDIT_INTERVAL_SECONDS).
Auto-correction: when internal total exceeds Asaas balance (gateway fees consumed),
the diff is automatically debited from the Matrix account to restore consistency.
Safe for production: fully async, fail-silent, structured logs only.
"""
import asyncio
from decimal import Decimal
from typing import Optional

from app.core.logger import audit_log, logger

_AUDIT_INTERVAL_SECONDS = 60  # 1 minute


async def _run_single_audit(db_factory, gateway_factory) -> dict:
    """
    Executes one balance audit cycle with auto-correction.

    When internal total exceeds Asaas balance (diff > 0), the divergence is caused
    by gateway fees (e.g. Asaas per-transaction cost) that reduced the Asaas account
    but were not reflected internally. The Matrix account is debited by the diff to
    restore parity.

    Returns a dict with status, totals, diff and correction metadata.
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

            internal_sum = sum(float(u.balance) for u in customers)
            matrix_balance = float(matrix_user.balance) if matrix_user else 0.0
            total_internal = internal_sum + matrix_balance
        finally:
            db.close()

        asaas_balance: Optional[float] = None
        gateway = gateway_factory()
        if gateway and hasattr(gateway, "_make_request"):
            try:
                resp = gateway._make_request("GET", "/finance/balance")
                asaas_balance = float(resp.get("balance", 0))
            except Exception as gw_err:
                logger.warning(f"[audit-worker] Asaas balance fetch failed: {gw_err}")

        signed_diff = round(total_internal - asaas_balance, 2) if asaas_balance is not None else None
        abs_diff = abs(signed_diff) if signed_diff is not None else None

        result = {
            "customers": len(customers),
            "internal_sum": round(internal_sum, 2),
            "matrix_balance": round(matrix_balance, 2),
            "total_internal": round(total_internal, 2),
            "asaas_balance": round(asaas_balance, 2) if asaas_balance is not None else None,
            "diff": abs_diff,
            "signed_diff": signed_diff,
            "status": "OK",
            "correction_applied": None,
        }

        if asaas_balance is None:
            result["status"] = "WARN_NO_GATEWAY"
            logger.info(
                "[audit-worker] Partial audit (no Asaas). "
                f"internal_total=R${total_internal:.2f} matrix=R${matrix_balance:.2f} customers={len(customers)}"
            )
        elif abs_diff < 0.01:
            result["status"] = "OK"
            logger.info(
                f"[audit-worker] PASS — internal=R${total_internal:.2f} asaas=R${asaas_balance:.2f} diff=R${abs_diff:.2f}"
            )
        else:
            # Divergence detected — attempt auto-correction when internal > asaas
            _AUTO_CORRECTION_MAX = 20.0  # only auto-correct diffs up to R$20 (max ~10 Asaas fees)
            if signed_diff > 0.01 and abs_diff <= _AUTO_CORRECTION_MAX and matrix_user is not None:
                # Internal exceeds Asaas by a small amount: Asaas gateway fees consumed balance.
                # Debit Matrix to reconcile.
                db2 = db_factory()
                try:
                    from app.auth.models import User as _User
                    live_matrix = db2.query(_User).filter(_User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
                    if live_matrix and float(live_matrix.balance) >= abs_diff:
                        live_matrix.balance = round(float(live_matrix.balance) - abs_diff, 2)
                        db2.commit()
                        result["correction_applied"] = {
                            "action": "matrix_debited",
                            "amount": abs_diff,
                            "matrix_balance_after": float(live_matrix.balance),
                        }
                        audit_log(
                            action="AUDIT_AUTO_CORRECTION",
                            user="audit-worker",
                            resource="matrix_balance",
                            details={
                                "diff": abs_diff,
                                "direction": "internal_above_asaas",
                                "matrix_balance_after": float(live_matrix.balance),
                                "asaas_balance": asaas_balance,
                            },
                        )
                        logger.info(
                            f"[audit-worker] AUTO-CORRECTION applied: Matrix debited R${abs_diff:.2f} "
                            f"new_matrix=R${live_matrix.balance:.2f}"
                        )
                        result["status"] = "AUTO_CORRECTED"
                    else:
                        result["status"] = "ERROR" if abs_diff >= 10 else "WARN"
                        logger.warning(
                            f"[audit-worker] Cannot auto-correct: insufficient Matrix balance "
                            f"(matrix=R${float(live_matrix.balance) if live_matrix else 0:.2f} diff=R${abs_diff:.2f})"
                        )
                finally:
                    db2.close()
            else:
                # diff > R$20 (structural imbalance) or Asaas > internal — log only, no auto-correct
                result["status"] = "ERROR" if abs_diff >= 10 else "WARN"
                logger.warning(
                    f"[audit-worker] DIVERGENCE (no-autocorrect) direction={direction if signed_diff > 0 else 'asaas_above_internal'} "
                    f"diff=R${abs_diff:.2f} internal=R${total_internal:.2f} asaas=R${asaas_balance:.2f}"
                )

        return result

    except Exception as exc:
        logger.error(f"[audit-worker] Unexpected error in audit cycle: {exc}")
        return {"status": "EXCEPTION", "error": str(exc)}


async def balance_audit_loop(db_factory, gateway_factory, interval: int = _AUDIT_INTERVAL_SECONDS):
    """
    Infinite async loop that runs the balance audit on a fixed interval.

    Args:
        db_factory: Callable that returns a SQLAlchemy Session (e.g. SessionLocal).
        gateway_factory: Callable that returns a PaymentGatewayPort instance or None.
        interval: Sleep duration in seconds between cycles (default 60 = 1 min).
    """
    logger.info(f"[audit-worker] Started. Interval: {interval}s")
    # Initial delay to let the app fully boot before first audit
    await asyncio.sleep(15)
    while True:
        await _run_single_audit(db_factory, gateway_factory)
        await asyncio.sleep(interval)
