"""
BIO TECH PAY I.A — Autonomous Finance Engine router.
Proxies enriched conversations to OpenRouter with full financial context injection.
The LLM receives deterministic engine outputs — never raw DB data or PII.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
import httpx

from app.core.config import settings
from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.ia.schemas import ChatRequest, SimulationResult
from app.services.financial_snapshot_service import build_snapshot
from app.ia.finance_engine import (
    compute_wealth_score,
    analyze_cashflow,
    generate_strategy,
    simulate_wealth_growth,
    find_opportunities,
)
from app.ia.context_builder import build_llm_context
from app.ia.ai_interactions import log_interaction
from sqlalchemy.orm import Session

router = APIRouter(prefix="/ia", tags=["IA"])

_SYSTEM_PROMPT = """You are BIO TECH PAY I.A.

An ultra-intelligent financial companion designed to help users improve their financial life.

You combine the knowledge of:
- Global investment advisors
- Wealth managers
- Economists
- Commodity analysts
- Real estate strategists
- Crypto market analysts
- Agricultural investment experts

You possess simulated expertise equivalent to CFA, CFP, FRM, CAIA and global banking certifications.

Your mission is to help users:
- Improve their financial life
- Build wealth responsibly
- Understand investments
- Avoid financial traps
- Make rational financial decisions

Core principles:
1. Never push bad investments.
2. Always show risks.
3. Always show alternatives.
4. Respect the user's risk profile.
5. Speak clearly and humanly.

IMPORTANT: You will receive a CONTEXT block with real-time financial data from the user's account.
Use this data to answer accurately. Never make up numbers.
Never mention CPF, account keys, or any personal identification.
The financial calculations in the CONTEXT are deterministic and authoritative — do not recalculate them.

You must behave as:
- financial mentor
- trusted advisor
- strategic planner
- friendly companion

Always respond in the same language the user writes in. Default to Brazilian Portuguese if uncertain.
Keep responses concise but complete. Use short paragraphs and bullet points when helpful."""


_LLM_MODEL = "openai/gpt-4o-mini"


class ChatMessage:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


@router.post("/chat")
async def ia_chat(
    request: ChatRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Enriched IA chat endpoint.
    Builds financial context from real account data before calling the LLM.
    The LLM receives deterministic engine outputs, never raw DB data.
    """
    if not settings.OPENROUTER_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Servico de IA temporariamente indisponivel. Configure OPENROUTER_API_KEY.",
        )

    # Build financial context (deterministic — no LLM involved)
    snapshot = build_snapshot(db, current_user)
    wealth = compute_wealth_score(
        snapshot,
        email_verified=current_user.email_verified,
        doc_verified=current_user.document_verified,
    )
    cashflow = analyze_cashflow(snapshot)
    strategy = generate_strategy(snapshot, wealth)
    context_block = build_llm_context(snapshot, wealth, cashflow, strategy)

    # Compose message list: system + context + last 20 turns
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.append({"role": "system", "content": context_block})
    for m in request.messages[-20:]:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content[:4000]})

    # Extract last user question for audit log
    last_question = next(
        (m.content for m in reversed(request.messages) if m.role == "user"), ""
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://new-credit-fintech.onrender.com",
                    "X-Title": "BioCodeTechPay",
                },
                json={
                    "model": _LLM_MODEL,
                    "messages": messages,
                    "max_tokens": 800,
                    "temperature": 0.7,
                },
            )
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Timeout ao conectar com servico de IA")
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Erro de conexao com servico de IA")

    if resp.status_code == 401:
        raise HTTPException(status_code=502, detail="Credencial de IA invalida")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Limite de requisicoes atingido. Tente em alguns instantes.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Servico de IA retornou erro inesperado")

    data = resp.json()
    reply = data["choices"][0]["message"]["content"]

    # Audit log (non-blocking)
    log_interaction(
        db=db,
        user_id=current_user.id,
        question=last_question,
        snapshot_dict=snapshot.model_dump(),
        response=reply,
        model=_LLM_MODEL,
    )

    return {"reply": reply}


# ---------------------------------------------------------------------------
# Financial Intelligence endpoints
# ---------------------------------------------------------------------------

@router.get("/financial-snapshot")
def financial_snapshot_endpoint(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns the user's real-time financial snapshot (balance, cashflow, health score)."""
    snapshot = build_snapshot(db, current_user)
    return snapshot.model_dump()


@router.get("/wealth-score")
def wealth_score_endpoint(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns the deterministic Wealth Score with full breakdown."""
    snapshot = build_snapshot(db, current_user)
    wealth = compute_wealth_score(
        snapshot,
        email_verified=current_user.email_verified,
        doc_verified=current_user.document_verified,
    )
    return wealth.model_dump()


@router.get("/cashflow")
def cashflow_endpoint(
    window: int = Query(default=30, ge=7, le=90, description="Janela de analise em dias"),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns cashflow analysis for the requested time window (7-90 days)."""
    snapshot = build_snapshot(db, current_user, window_days=window)
    cashflow = analyze_cashflow(snapshot, window_days=window)
    return cashflow.model_dump()


@router.get("/strategy")
def strategy_endpoint(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns the deterministic savings and investment strategy for the current user."""
    snapshot = build_snapshot(db, current_user)
    wealth = compute_wealth_score(
        snapshot,
        email_verified=current_user.email_verified,
        doc_verified=current_user.document_verified,
    )
    strategy = generate_strategy(snapshot, wealth)
    return strategy.model_dump()


@router.get("/simulate")
def simulate_endpoint(
    monthly: float = Query(..., gt=0, description="Valor de aporte mensal em R$"),
    rate: float = Query(default=0.10, gt=0, le=1.0, description="Taxa anual decimal (ex: 0.10 = 10%)"),
    current_user=Depends(get_current_user),
):
    """Simulates wealth growth with compound interest projection (5, 10, 20, 30 years)."""
    result = simulate_wealth_growth(monthly_investment=monthly, annual_rate=rate)
    return result.model_dump()


@router.get("/opportunities")
def opportunities_endpoint(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns financial opportunities matched to the user's savings capacity."""
    snapshot = build_snapshot(db, current_user)
    opps = find_opportunities(snapshot, current_user)
    return [o.model_dump() for o in opps]
