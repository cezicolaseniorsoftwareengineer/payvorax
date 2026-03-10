"""
Balance Audit Worker.

Periodic background task that compares the sum of all internal user balances
against the Asaas account balance to detect discrepancies early.

Run cycle: every 30 minutes by default (configurable via AUDIT_INTERVAL_SECONDS).
Safe for production: fully async, fail-silent, structured logs only.
"""
import asyncio
import logging
from typing import Optional

from app.core.logger import logger

_AUDIT_INTERVAL_SECONDS = 1800  # 30 minutes


async def _run_single_audit(db_factory, gateway_factory) -> dict:
    """
    Executes one balance audit cycle.

    Returns a dict with status, totals and optional diff.
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

        result = {
            "customers": len(customers),
            "internal_sum": round(internal_sum, 2),
            "matrix_balance": round(matrix_balance, 2),
            "total_internal": round(total_internal, 2),
            "asaas_balance": round(asaas_balance, 2) if asaas_balance is not None else None,
            "diff": round(abs(total_internal - asaas_balance), 2) if asaas_balance is not None else None,
            "status": "OK",
        }

        if asaas_balance is None:
            result["status"] = "WARN_NO_GATEWAY"
            logger.info(
                "[audit-worker] Partial audit (no Asaas). "
                f"internal_total=R${total_internal:.2f} matrix=R${matrix_balance:.2f} customers={len(customers)}"
            )
        elif result["diff"] < 0.01:
            result["status"] = "OK"
            logger.info(
                f"[audit-worker] PASS — internal=R${total_internal:.2f} asaas=R${asaas_balance:.2f} diff=R${result['diff']:.2f}"
            )
        elif result["diff"] < 10:
            result["status"] = "WARN"
            logger.warning(
                f"[audit-worker] WARN — diff=R${result['diff']:.2f} "
                f"internal=R${total_internal:.2f} asaas=R${asaas_balance:.2f}"
            )
        else:
            result["status"] = "ERROR"
            logger.error(
                f"[audit-worker] CRITICAL DIVERGENCE — diff=R${result['diff']:.2f} "
                f"internal=R${total_internal:.2f} asaas=R${asaas_balance:.2f} "
                f"matrix=R${matrix_balance:.2f} customers_sum=R${internal_sum:.2f}"
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
        interval: Sleep duration in seconds between cycles (default 1800 = 30 min).
    """
    logger.info(f"[audit-worker] Started. Interval: {interval}s")
    # Initial delay to let the app fully boot before first audit
    await asyncio.sleep(15)
    while True:
        await _run_single_audit(db_factory, gateway_factory)
        await asyncio.sleep(interval)
