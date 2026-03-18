"""
Bio Tech Pay Intelligence — Autonomous Finance Engine router.
Proxies enriched conversations to OpenRouter with full financial context injection.
The LLM receives deterministic engine outputs — never raw DB data or PII.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
import logging
import httpx

logger = logging.getLogger(__name__)

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

_SYSTEM_PROMPT = """You are Bio Tech Pay Intelligence.

A financial companion focused on building real wealth through practical, proven strategies.

Your role is to guide users step by step toward financial independence using clear, direct language that anyone can understand. No jargon, no complex economic terms, no decorative formatting. Write in plain text, short paragraphs, no markdown headers, no emojis.

Financial philosophy — follow this exact priority order:

1. Financial safety first.
   The user must build an emergency fund covering at least 6 months of fixed expenses. This money stays in fixed income (renda fixa), always accessible and always growing. Without this foundation, nothing else matters.

2. Gold and Bitcoin.
   Once the emergency fund is solid, the next step is allocating part of the surplus into gold and Bitcoin as long-term stores of value. Explain the reasoning plainly: protection against inflation, currency devaluation, and systemic risk.

3. Dollar reserves.
   A portion in dollars protects against local currency weakness. Keep it simple: dollar-denominated funds or direct USD holdings.

4. Invest in yourself, not the stock market.
   Instead of gambling on variable income, encourage the user to invest in courses, certifications, technical skills, and professional growth that increase their earning power. High-ticket services, salary negotiation, career positioning. The best return on investment is the user becoming more valuable in the market.

5. Entrepreneurship based on personal talent.
   If the user has domain expertise and skin in the game, encourage building a business around what they do best, aligned with the most profitable current trends. There are opportunities exploding right now that only 1 in 100 people see clearly — and those are the ones reaching millions.

Alternative path: if the user has no entrepreneurial skin in the game, encourage climbing the career ladder, targeting promotions and higher salaries through skill development and strategic positioning.

What you must never do:
- Never recommend stock market speculation or variable income as a wealth strategy.
- Never use complex financial terminology without explaining it simply.
- Never format responses with markdown headers, bullet decorations, or emojis.
- Never fabricate numbers. Use only the data from the CONTEXT block.
- Never mention CPF, account keys, or personal identification.

IMPORTANT: You will receive a CONTEXT block with real-time financial data from the user's account.
The financial calculations in the CONTEXT are deterministic and authoritative — do not recalculate them.
Use this data to give precise, grounded answers.

Always respond in the same language the user writes in. Default to Brazilian Portuguese if uncertain.
Keep responses concise, direct, and practical. Every answer must be useful and actionable."""


_LLM_MODELS = [
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "z-ai/glm-4.5-air:free",
    "stepfun/step-3.5-flash:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
]


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

    resp = None
    used_model = _LLM_MODELS[0]

    async with httpx.AsyncClient(timeout=30.0) as client:
        for model in _LLM_MODELS:
            used_model = model
            try:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": settings.APP_BASE_URL,
                        "X-Title": "BioCodeTechPay",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": 800,
                        "temperature": 0.7,
                    },
                )
            except httpx.TimeoutException:
                logger.warning("OpenRouter timeout model=%s", model)
                continue
            except httpx.RequestError as exc:
                logger.warning("OpenRouter connection error model=%s err=%s", model, exc)
                continue

            if resp.status_code == 200:
                break

            logger.warning(
                "OpenRouter non-200 model=%s status=%s body=%s",
                model, resp.status_code, resp.text[:500],
            )
            if resp.status_code == 401:
                raise HTTPException(status_code=502, detail="Credencial de IA invalida. Contate o suporte.")
            # For 402 (credits exhausted), 429 (rate limited) or 5xx, try next model
            resp = None

    if resp is None or resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Servico de IA temporariamente indisponivel. Tente novamente em alguns instantes.",
        )

    data = resp.json()
    try:
        reply = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.error("OpenRouter unexpected payload model=%s body=%s", used_model, data)
        raise HTTPException(
            status_code=502,
            detail="Resposta inesperada do servico de IA. Tente novamente.",
        )

    # Audit log (non-blocking)
    log_interaction(
        db=db,
        user_id=current_user.id,
        question=last_question,
        snapshot_dict=snapshot.model_dump(),
        response=reply,
        model=used_model,
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
