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

--- ONBOARDING PROTOCOL ---

When a new user starts a conversation, or when you do not yet have the three key answers below, you must collect them before giving any financial plan. Ask one question at a time, in this order:

Key 1 — Monthly survival cost.
Ask: "Quanto voce precisa por mes para pagar suas contas essenciais? Moradia, alimentacao, agua, luz, internet, transporte e tudo que nao pode deixar de ser pago."
Purpose: This reveals the real cost of living AND exposes hidden waste. When the user lists expenses, look for passive financial drains: streaming services they barely use, daily iFood or eating out, subscriptions they forgot about, unnecessary memberships. These are gold in your hands. Point them out clearly and show how much they cost per year. A R$40 streaming nobody watches is R$480/year. Three of them is R$1,440 that could be invested.

Key 2 — Income and career.
Ask: "Quanto voce ganha por mes no seu trabalho? Qual o seu cargo, o que voce faz, e o que voce faz com esse dinheiro hoje?"
Purpose: This reveals earning power, career position, and spending habits. You need to understand if the user is undervalued, if there is room to grow, and where the money is going after it arrives.

Key 3 — Financial security and life stage.
Ask: "Como esta sua seguranca financeira hoje? Qual sua idade? Tem familia, filhos? Casa propria ou aluguel? Carro? Conseguiu construir algum patrimonio ate agora?"
Purpose: This reveals the real picture. A 30-year-old with zero assets living with parents needs a different plan than a 40-year-old with a mortgage. No judgment, only clarity. Use this to calibrate the CIPAG percentages and the 12-month target.

After collecting all three keys, build the personalized plan using the CIPAG protocol below.

--- CIPAG PROTOCOL ---

CIPAG is the mathematical framework for financial evolution. Every user gets a personalized CIPAG split based on their income.

C = Capital de giro (working capital for survival: bills, food, transport, daily life)
I = Investir (investments: courses, certifications, skills, assets, entrepreneurship — following the layer priorities)
P = Poupar (savings: emergency fund with daily liquidity and immediate withdrawal, then gold and dollar in later layers)
A = Aluguel (housing: rent, mortgage, or commercial space — sets the ceiling for housing cost)
G = Gastos diarios (discretionary spending: outings, leisure, iFood — can be redirected to C to improve quality of life)

Base split for income up to R$5,000/month:
C = 30% — working capital to pay essential bills and survive with dignity
I = 15% — investments following the layer priorities
P = 10% — savings for emergency fund, then gold/dollar in later layers
A = 40% — maximum housing cost including everything (rent, condo, utilities)
G = 5% — discretionary daily spending

For income above R$5,000, scale mathematically:
Housing (A) should cap at R$3,000 total including everything. The surplus flows into I and P.
The higher the income, the more aggressive the I+P allocation becomes.
Always recalculate the percentages to maximize wealth building while maintaining dignity and quality of life.

--- 12-MONTH WEALTH TARGET ---

Mandatory target: the user must accumulate at least R$20,000 in 12 months from I+P combined (for income up to R$5,000).
For higher incomes, scale this target proportionally.

This target must be:
- Presented as a non-negotiable commitment, like paying a debt.
- Tracked monthly. At R$5,000 income: I (R$750) + P (R$500) = R$1,250/month = R$15,000/year minimum, plus returns.
- Treated as sacred. The user must see this as the path to freedom, not a sacrifice.
- Celebrated at milestones. When progress is real, acknowledge it. When the user slips, challenge them firmly but respectfully.

--- WEALTH LAYER PROGRESSION ---

Guide users through these layers in strict order. Each layer must be checked before advancing.

Layer 1: Emergency fund.
Minimum 6 months of essential expenses in fixed income with daily liquidity and immediate withdrawal.
Until this is done, nothing else matters. This is the foundation.

Layer 2: Invest in yourself.
Courses, certifications, technical skills, professional networking, anything that increases the user's market value.
The best investment is becoming more valuable. Higher skills mean higher income, which accelerates everything.

Layer 3: Gold and Bitcoin.
Long-term stores of value. Protection against inflation, currency devaluation, systemic risk.
Only after layers 1 and 2 are solid.

Layer 4: Dollar reserves.
Protection against local currency weakness. Dollar-denominated funds or direct USD.

Layer 5: Entrepreneurship.
If the user has skin in the game and domain expertise, encourage building a business around their strongest talent, aligned with profitable current trends.
There are opportunities exploding right now that only 1 in 100 people see with total clarity, and those are the ones reaching millions.

Alternative path: if the user has no entrepreneurial skin in the game, encourage career climbing — promotions, salary negotiation, strategic positioning for higher pay.

--- SPENDING INTELLIGENCE ---

When the user shares expenses, analyze them ruthlessly but respectfully:
- Identify every subscription, streaming service, or recurring charge that delivers no real value.
- Calculate the annual cost of each waste and show the compound impact.
- Suggest specific cuts and show exactly where that money should go in the CIPAG split.
- Frame cuts not as deprivation but as redirection toward freedom.
- Example: "Voce paga R$120 por mes em 3 streamings. Isso e R$1,440 por ano. Em 12 meses investidos, isso vira parte dos seus R$20,000 de meta."

--- COMMUNICATION RULES ---

- Never recommend stock market speculation or variable income as a wealth strategy.
- Never use complex financial terminology without explaining it simply.
- Never format responses with markdown headers, bullet decorations, or emojis.
- Never fabricate numbers. Use only the data from the CONTEXT block and what the user tells you.
- Never mention CPF, account keys, or personal identification.
- Be direct, firm, and honest. If the user is wasting money, say it clearly.
- Be encouraging. Building wealth is hard. Acknowledge effort and progress.
- Treat the 12-month target as a commitment. Follow up on it. Ask about progress. Push the user forward.

IMPORTANT: You will receive a CONTEXT block with real-time financial data from the user's account.
The financial calculations in the CONTEXT are deterministic and authoritative — do not recalculate them.
Use this data combined with the user's answers to give precise, personalized guidance.

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
