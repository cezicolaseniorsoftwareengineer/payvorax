"""
Executa um ciclo manual do audit worker para reconciliar DB com Asaas agora.
Qualquer residual vai para a Conta Matrix (conta de taxas da plataforma).
"""
import asyncio
import sys
sys.path.insert(0, '.')

# Import CreditCard first to resolve SQLAlchemy relationship before any User query
from app.cards.models import CreditCard  # noqa: F401
from app.core.database import SessionLocal
from app.core.audit_worker import _run_single_audit
from app.adapters.asaas_adapter import AsaasAdapter
from app.core.config import settings


def _get_gateway():
    try:
        return AsaasAdapter(api_key=settings.ASAAS_API_KEY)
    except Exception:
        return None


async def main():
    print("Iniciando ciclo manual de auditoria...")
    result = await _run_single_audit(SessionLocal, _get_gateway)

    print(f"\nStatus:           {result['status']}")
    print(f"Saldo clientes:   R${result['internal_sum']:.2f}")
    print(f"Saldo Matrix:     R${result['matrix_balance']:.2f}")
    print(f"Total interno:    R${result['total_internal']:.2f}")
    asaas = result.get('asaas_balance')
    print(f"Asaas real:       R${asaas:.2f}" if asaas is not None else "Asaas: indisponivel")
    diff = result.get('diff') or 0
    print(f"Divergencia:      R${diff:.2f}")

    correction = result.get('correction_applied')
    if correction:
        print(f"\nCorrecao aplicada: {correction}")

    ai = result.get('ai_analysis')
    if ai:
        print(f"\nAnalise IA:\n{ai}")

    print("\nDone.")


asyncio.run(main())
