"""
Context Builder — transforms engine outputs into a structured LLM context block.
The LLM receives only computed results. No raw DB data, no PII.
"""
from app.ia.schemas import FinancialSnapshot, WealthScore, CashflowWindow, StrategyPlan


def build_llm_context(
    snapshot: FinancialSnapshot,
    wealth: WealthScore,
    cashflow: CashflowWindow,
    strategy: StrategyPlan,
) -> str:
    """
    Builds the CONTEXT block injected between system prompt and user message.
    Compact plaintext — never includes CPF, names, keys or account IDs.
    """
    alerts_str = "; ".join(cashflow.alerts) if cashflow.alerts else "nenhum"
    notes_str = " | ".join(strategy.notes) if strategy.notes else ""

    return (
        "--- CONTEXTO FINANCEIRO (dados reais da conta, tempo real) ---\n"
        f"Saldo atual: R$ {snapshot.balance:.2f}\n"
        f"Últimos 30 dias:\n"
        f"  Recebido:         R$ {snapshot.last_30d_received:.2f}\n"
        f"  Gasto:            R$ {snapshot.last_30d_sent:.2f}\n"
        f"  Líquido (net):    R$ {snapshot.net_cashflow:.2f}\n"
        f"  Taxa de poupança: {snapshot.savings_rate * 100:.1f}%\n"
        f"  Transações:       {snapshot.total_transactions_30d}\n"
        f"\n"
        f"Wealth Score: {wealth.score}/100 ({wealth.label})\n"
        f"  Breakdown: poupança {wealth.savings_rate_score}/40 | liquidez {wealth.liquidity_score}/30 | "
        f"atividade {wealth.activity_score}/15 | verificação {wealth.verification_score}/15\n"
        f"  Capacidade de poupança mensal: R$ {wealth.savings_capacity:.2f}\n"
        f"  Cobertura de emergência atual: {wealth.emergency_fund_months:.1f} meses\n"
        f"\n"
        f"Cashflow (30 dias):\n"
        f"  Gasto médio diário: R$ {cashflow.avg_daily_outbound:.2f}\n"
        f"  Burn rate:          {cashflow.burn_rate_days:.0f} dias de runway\n"
        f"  Alertas: {alerts_str}\n"
        f"\n"
        f"Estratégia recomendada (determinística):\n"
        f"  Prioridade:                 {strategy.priority}\n"
        f"  Poupança mensal recomendada: R$ {strategy.monthly_savings_target:.2f}\n"
        f"  Reserva de emergência pendente: R$ {strategy.emergency_fund_remaining:.2f}\n"
        f"  Investimento sugerido:      {strategy.investment_suggestion}\n"
        + (f"  Notas: {notes_str}\n" if notes_str else "")
        + "--- FIM DO CONTEXTO ---"
    )
