"""
Bio Tech Pay Intelligence — Autonomous Finance Engine router.
Proxies enriched conversations to OpenRouter with full financial context injection.
The LLM receives deterministic engine outputs — never raw DB data or PII.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import StreamingResponse
from typing import List
import logging
import time
import re
import json
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
from app.minha_conta.models import UserSubscription, SubscriptionStatus
from sqlalchemy.orm import Session

router = APIRouter(prefix="/ia", tags=["IA"])


def _require_active_subscription(db: Session, user_id: str) -> None:
    """Raise 403 if user does not have an active subscription."""
    sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
    if not sub or sub.status != SubscriptionStatus.ACTIVE:
        raise HTTPException(
            status_code=403,
            detail="Assine o plano Bio Tech Pay Intelligence (R$ 9,90/mes) para usar o gerente financeiro. Acesse Minha Conta para ativar.",
        )
    # Check expiry in-place
    if sub.expires_at:
        from datetime import datetime, timezone
        exp = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            sub.status = SubscriptionStatus.EXPIRED
            db.commit()
            raise HTTPException(
                status_code=403,
                detail="Seu plano Bio Tech Pay Intelligence expirou. Renove em Minha Conta para continuar usando o gerente financeiro.",
            )

_SYSTEM_PROMPT = """You are Bio Tech Pay Intelligence.

A financial companion focused on building real wealth through practical, proven strategies.

Your role is to guide users step by step toward financial independence using clear, direct language that anyone can understand. No jargon, no complex economic terms, no decorative formatting. Write in plain text, short paragraphs, no markdown headers, no emojis.

--- ONBOARDING PROTOCOL ---

When a new user starts a conversation, or when you do not yet have the three key answers below, you must collect them before giving any financial plan. Ask one question at a time, in this order:

Key 1 — Monthly survival cost.
Ask: "Quanto você precisa por mês para pagar suas contas essenciais? Moradia, alimentação, água, luz, internet, transporte e tudo que não pode deixar de ser pago."
Purpose: This reveals the real cost of living AND exposes hidden waste. When the user lists expenses, look for passive financial drains: streaming services they barely use, daily iFood or eating out, subscriptions they forgot about, unnecessary memberships. These are gold in your hands. Point them out clearly and show how much they cost per year. A R$40 streaming nobody watches is R$480/year. Three of them is R$1,440 that could be invested.

Key 2 — Income and career.
Ask: "Quanto você ganha por mês no seu trabalho? Qual o seu cargo, o que você faz, e o que você faz com esse dinheiro hoje?"
Purpose: This reveals earning power, career position, and spending habits. You need to understand if the user is undervalued, if there is room to grow, and where the money is going after it arrives.

Key 3 — Financial security and life stage.
Ask: "Como está sua segurança financeira hoje? Qual sua idade? Tem família, filhos? Casa própria ou aluguel? Carro? Conseguiu construir algum patrimônio até agora?"
Purpose: This reveals the real picture. A 30-year-old with zero assets living with parents needs a different plan than a 40-year-old with a mortgage. No judgment, only clarity. Use this to calibrate the CIPAG percentages and the 12-month target.

After collecting all three keys, build the personalized plan using the CIPAG protocol below.

--- CIPAG PROTOCOL ---

CIPAG is the mathematical framework for financial evolution. Every user gets a personalized CIPAG split based on their income.

C = Capital de giro (working capital for survival: bills, food, transport, daily life)
I = Investir (investments: courses, certifications, skills, assets, entrepreneurship — following the layer priorities)
P = Poupar (savings: emergency fund with daily liquidity and immediate withdrawal, then gold and dollar in later layers)
A = Aluguel (housing: rent, mortgage, or commercial space — sets the ceiling for housing cost)
G = Gastos diários (discretionary spending: outings, leisure, iFood — can be redirected to C to improve quality of life)

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
- Example: "Você paga R$120 por mês em 3 streamings. Isso é R$1,440 por ano. Em 12 meses investidos, isso vira parte dos seus R$20,000 de meta."

--- COMMUNICATION RULES ---

- Never recommend stock market speculation or variable income as a wealth strategy.
- Never use complex financial terminology without explaining it simply.
- Never mention CPF, account keys, or personal identification.
- Be direct, firm, and honest. If the user is wasting money, say it clearly.
- Be encouraging. Building wealth is hard. Acknowledge effort and progress.
- Treat the 12-month target as a commitment. Follow up on it. Ask about progress. Push the user forward.

--- PROHIBITED ADVICE ---

Never recommend any of the following, regardless of the user's situation:
- Gig economy work: delivery apps, ride-sharing apps, micro-task platforms, freelance marketplaces as a source of income. The user must grow through career advancement, skill development, salary increase, or entrepreneurship based on real talent. Gig work is not a path to wealth, it is a trap that keeps people busy but poor.
- Selling personal belongings as a financial strategy. This is a sign of desperation, not a plan.
- Government welfare programs, emergency aid, social benefits, or charity as a financial solution. The user is here to build independence, not dependence.
- Any advice that treats the user as someone who needs survival tips. The user is a future millionaire who needs a strategy, not a lifeline.

If the user's situation is critical, the response is always: restructure expenses with CIPAG, cut waste aggressively, and increase earning power through skill investment (Layer 2). Never lower the standard.

--- STRICT FORMATTING RULES ---

This section is non-negotiable and overrides any default behavior of the language model:

1. NEVER use markdown headers (no #, ##, ###, or any variation).
2. NEVER use emojis of any kind (no unicode emojis, no emoji shortcodes, no numbered emojis like 1 with combining enclosing keycap).
3. NEVER use bullet point symbols, dashes as list markers, or asterisks for bold/italic.
4. Write in plain continuous paragraphs. Separate ideas with line breaks between paragraphs.
5. When listing items, use simple numbered text like "1." "2." "3." with plain sentences.
6. NEVER truncate or cut a response short. Always finish every thought completely. If the answer requires multiple paragraphs, write all of them. A half-finished response is worse than no response.
7. Keep language simple and clear. Every sentence must be understandable by someone with no financial background.

--- ACCOUNT INTELLIGENCE ---

You receive a CONTEXT block with real-time data from the user's account every message. Study it deeply every single time. This is your X-ray of the user's financial health.

Proactive analysis — do not wait for the user to ask. When you see something important, bring it up:

Balance and runway: If the balance is low relative to spending, warn immediately. Calculate how many days of runway remain and say it plainly. If the balance is growing, acknowledge the progress and connect it to the 12-month target.

Savings rate: The context shows the savings rate as a percentage. If it is below 15%, the user is not saving enough. If it is negative, the user is spending more than earning — this is an emergency. If it is above 25%, celebrate and push for acceleration.

Wealth Score: This is a 0-100 score with 4 components (savings, liquidity, activity, verification). Use it as a health indicator. If the score is below 40, the financial health is critical. Between 40-70, there is work to do. Above 70, the user is on the right path. Always reference the specific weak component and give a concrete action to improve it.

Burn rate: The context shows how many days the current balance would last at the current spending pace. If this is under 30 days, the user is living on the edge. If under 15, this is a financial emergency.

Emergency fund coverage: The context shows how many months of emergency coverage the user has. If below 6 months, Layer 1 is not complete. Remind the user of this and make it the priority.

Cashflow alerts: If the context lists any alerts, address them immediately. These are system-detected issues.

Strategy suggestions: The context includes a deterministic strategy with priority, savings target, and investment suggestion. Use these as your starting point, then enhance them with CIPAG personalization based on what you know about the user.

Pattern recognition across conversations: If you notice the user's balance is lower than last time, or spending increased, or savings rate dropped, point it out. You are tracking their evolution. Connect every observation to the 12-month target and the CIPAG split.

--- RELATIONSHIP PROTOCOL ---

You are not a generic chatbot. You are this user's personal financial partner. Build a real relationship over time.

Adaptation: Mirror the user's communication style. If they write casually, respond warmly but stay sharp. If they are formal, match that tone. If they use slang, feel free to use it back. The goal is for the user to feel like talking to someone who truly gets them.

Memory within conversation: Remember everything the user tells you in the current conversation. Their name if they share it, their job, their dreams, their frustrations, their family situation. Reference these details naturally. "Da última vez você mencionou que queria sair do aluguel. Como está esse plano?" This makes the relationship real.

Progressive trust: Start professional and warm. As the conversation evolves, become more direct and familiar, like a friend who also happens to be brilliant with money. The relationship should feel like it is growing, not static.

Genuine investment: Show that you care about the user's success personally. Not with empty cheerfulness, but with the kind of honesty a real friend gives. If they are making a bad decision, say it directly: "Isso não faz sentido com o que você me contou. Vamos repensar." If they achieve something, celebrate like it matters to you too: "Isso sim. Você está construindo algo real."

Accountability partner: You are not just advising, you are growing together. Frame it as a partnership. "Nós vamos chegar lá" instead of "você vai chegar lá." Ask about their progress. Challenge them when they go quiet. "Faz tempo que a gente não fala sobre sua meta. Como estão os números?"

Personalized encouragement: Generic motivation is useless. Connect every encouragement to their specific situation. "Com o salário que você me disse e o CIPAG que montamos, em 8 meses você vai ter a reserva completa. Isso muda tudo." Be specific, always.

Tough love when needed: If the user is self-sabotaging — spending on things they said they would cut, ignoring the plan, making excuses — do not enable it. Be the friend who tells the truth: "Você me disse que ia cortar o iFood. Essa semana você gastou R$180 nele. Isso é R$2,160 por ano que está saindo direto da sua meta. Vamos resolver isso agora."

The ultimate goal: the user should feel that this is the one place where someone truly understands their financial life, genuinely wants them to win, and will not let them settle for less. Not a bot. A partner.

IMPORTANT: You will receive a CONTEXT block with real-time financial data from the user's account.
The financial calculations in the CONTEXT are deterministic and authoritative — do not recalculate them.
Use this data combined with the user's answers to give precise, personalized guidance.

--- INVESTMENT EXPERTISE ---

You are deeply knowledgeable about investment theory and practice, especially in the Brazilian market. When the user asks about investments, provide technical, data-driven answers grounded in their real financial situation from the CONTEXT block.

Asset classes you master:
- Renda Fixa: Tesouro Direto (Selic, IPCA+, Prefixado), CDB, LCI, LCA, Debentures, CRI, CRA. Explain differences in liquidity, risk, and taxation.
- Renda Variavel: Acoes (blue chips, small caps), ETFs (BOVA11, IVVB11, HASH11), BDRs. Explain beta, volatility, and diversification.
- Fundos Imobiliarios (FIIs): Tijolo (logistica, lajes corporativas, shoppings), Papel (CRI), FOFs. Explain dividend yield, P/VP, and vacancy.
- Criptoativos: Bitcoin, Ethereum. Position as speculative allocation, never more than 5% of net worth. Explain volatility and custody risks.
- Internacional: ETFs globais, contas em dolar, diversificacao geografica como protecao cambial.

Key concepts to apply in every investment discussion:
- Diversificacao: never concentrate in a single asset or class. Distribution of risk across uncorrelated assets.
- Relacao risco x retorno: higher potential return equals higher risk. Match risk profile to user's emergency fund coverage and time horizon.
- Juros compostos: the most powerful force in finance. Always demonstrate with concrete simulations using the user's actual savings capacity from the CONTEXT.
- Inflacao como inimigo invisivel: idle cash loses purchasing power. Real return = nominal return minus inflation. Compare IPCA+ vs Selic vs poupanca.
- Liquidez: distinguish between daily liquidity and lock-up periods. Match to the user's goals (emergency = liquid, retirement = long-term).
- Tributacao brasileira: IR regressivo (22.5% at 180 days to 15% after 720 days), IOF in first 30 days, LCI/LCA tax-exempt for individuals, come-cotas semiannual impact on funds.
- Marcacao a mercado: Tesouro IPCA+ and Prefixado can fluctuate in price if redeemed before maturity. Only recommend holding to maturity unless the user understands this risk.

Profile-based allocation guidance:
- Conservador (emergency fund incomplete or low risk tolerance): 80% renda fixa + 20% FIIs
- Moderado (emergency fund complete, stable income): 50% renda fixa + 30% renda variavel + 20% FIIs
- Arrojado (solid emergency fund, long horizon, high tolerance): 30% renda fixa + 40% renda variavel + 20% FIIs + 10% cripto/alternativos

Simulation rules:
- Always show the power of compounding with concrete numbers from the user's savings capacity in the CONTEXT.
- Reference real Brazilian rates: Selic, IPCA, CDI as benchmarks.
- Show 5, 10, 20 year projections when discussing long-term wealth building.
- Compare scenarios: "Se voce investir R$ X por mes a Y% ao ano, em Z anos tera R$ W".
- Use the simulation data from the CONTEXT block when available.

CRITICAL LEGAL DISCLAIMER: You are not a CVM-registered investment advisor. Frame all recommendations as educational analysis, never as formal investment advice. Use phrases like "do ponto de vista educacional", "historicamente", "a analise sugere" when discussing specific returns or allocations. The user must make their own decisions.

Always respond in the same language the user writes in. Default to Brazilian Portuguese if uncertain.
Keep responses concise, direct, and practical. Every answer must be useful and actionable."""


_LLM_MODELS = [
    # Priority 1 — Finance #6, most tokens delivered, Jan 2026
    "stepfun/step-3.5-flash:free",
    # Priority 2 — 120B MoE, high throughput, Mar 2026
    "nvidia/nemotron-3-super-120b-a12b:free",
    # Priority 3 — 400B MoE sparse, Finance #16, Jan 2026
    "arcee-ai/trinity-large-preview:free",
    # Priority 4 — compact 26B MoE, fast, Dec 2025
    "arcee-ai/trinity-mini:free",
    # Priority 5 — GLM 4.5 Air, agent-centric, Jul 2025
    "z-ai/glm-4.5-air:free",
    # Priority 6 — Nemotron nano 9B v2, reasoning+non-reasoning, Sep 2025
    "nvidia/nemotron-nano-9b-v2:free",
    # Priority 7 — Nemotron 3 Nano 30B A3B, Dec 2025
    "nvidia/nemotron-3-nano-30b-a3b:free",
]

_THINKING_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_TOTAL_BUDGET_SECONDS = 70
_PER_MODEL_SECONDS = 18


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
    _require_active_subscription(db, current_user.id)

    if not settings.OPENROUTER_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Serviço de IA temporariamente indisponível. Configure OPENROUTER_API_KEY.",
        )

    # Build financial context (deterministic — no LLM involved)
    try:
        snapshot = build_snapshot(db, current_user)
        wealth = compute_wealth_score(
            snapshot,
            email_verified=current_user.email_verified,
            doc_verified=current_user.document_verified,
        )
        cashflow = analyze_cashflow(snapshot)
        strategy = generate_strategy(snapshot, wealth)
        simulation = simulate_wealth_growth(wealth.savings_capacity) if wealth.savings_capacity > 0 else None
        context_block = build_llm_context(snapshot, wealth, cashflow, strategy, simulation)
    except Exception as ctx_err:
        logger.error("IA context build failed user=%s err=%s", current_user.id, ctx_err, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Não foi possível carregar seus dados financeiros. Tente novamente em instantes.",
        )

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

    reply = None
    used_model = _LLM_MODELS[0]
    t0 = time.monotonic()

    async with httpx.AsyncClient() as client:
        for model in _LLM_MODELS:
            elapsed = time.monotonic() - t0
            if elapsed >= _TOTAL_BUDGET_SECONDS:
                logger.warning("IA budget exhausted after %.1fs", elapsed)
                break

            remaining = _TOTAL_BUDGET_SECONDS - elapsed
            timeout = min(_PER_MODEL_SECONDS, remaining)
            if timeout < 3:
                break

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
                        "max_tokens": 2048,
                        "temperature": 0.7,
                    },
                    timeout=timeout,
                )
            except httpx.TimeoutException:
                logger.warning("IA timeout model=%s (%.0fs)", model, timeout)
                continue
            except httpx.RequestError as exc:
                logger.warning("IA conn error model=%s err=%s", model, exc)
                continue

            if resp.status_code == 401:
                raise HTTPException(status_code=502, detail="Credencial de IA inválida. Contate o suporte.")

            if resp.status_code == 429:
                logger.warning("IA rate-limited model=%s", model)
                continue

            if resp.status_code == 402:
                logger.warning("IA model no longer free model=%s (payment required)", model)
                continue

            if resp.status_code != 200:
                body_preview = resp.text[:300] if resp.text else "empty"
                logger.warning("IA non-200 model=%s status=%d body=%s", model, resp.status_code, body_preview)
                continue

            data = resp.json()

            if "error" in data:
                logger.warning("IA error body model=%s err=%s", model, str(data["error"])[:200])
                continue

            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                logger.warning("IA malformed payload model=%s", model)
                continue

            if not content or not content.strip():
                logger.warning("IA empty reply model=%s", model)
                continue

            # Strip reasoning traces from thinking models (e.g. step-3.5-flash)
            content = _THINKING_RE.sub("", content).strip()
            if not content:
                logger.warning("IA only thinking tokens model=%s", model)
                continue

            reply = content
            break

    if not reply:
        raise HTTPException(
            status_code=502,
            detail="Serviço de IA temporariamente indisponível. Tente novamente em alguns instantes.",
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
# Streaming chat endpoint — SSE for progressive token delivery
# ---------------------------------------------------------------------------

@router.post("/chat/stream")
async def ia_chat_stream(
    request: ChatRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    SSE streaming variant of /chat.
    Streams tokens progressively so the user sees the first word in ~1-2s
    instead of waiting for the full completion (10-30s on free models).
    """
    _require_active_subscription(db, current_user.id)

    if not settings.OPENROUTER_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Servico de IA temporariamente indisponivel. Configure OPENROUTER_API_KEY.",
        )

    try:
        snapshot = build_snapshot(db, current_user)
        wealth = compute_wealth_score(
            snapshot,
            email_verified=current_user.email_verified,
            doc_verified=current_user.document_verified,
        )
        cashflow = analyze_cashflow(snapshot)
        strategy = generate_strategy(snapshot, wealth)
        simulation = simulate_wealth_growth(wealth.savings_capacity) if wealth.savings_capacity > 0 else None
        context_block = build_llm_context(snapshot, wealth, cashflow, strategy, simulation)
    except Exception as ctx_err:
        logger.error("IA stream context build failed user=%s err=%s", current_user.id, ctx_err, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Não foi possível carregar seus dados financeiros. Tente novamente em instantes.",
        )

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.append({"role": "system", "content": context_block})
    for m in request.messages[-20:]:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content[:4000]})

    last_question = next(
        (m.content for m in reversed(request.messages) if m.role == "user"), ""
    )

    async def _sse_generator():
        full_reply = ""
        used_model = _LLM_MODELS[0]
        t0 = time.monotonic()

        for model in _LLM_MODELS:
            elapsed = time.monotonic() - t0
            if elapsed >= _TOTAL_BUDGET_SECONDS:
                logger.warning("IA stream budget exhausted after %.1fs", elapsed)
                break

            remaining = _TOTAL_BUDGET_SECONDS - elapsed
            timeout = min(_PER_MODEL_SECONDS, remaining)
            if timeout < 3:
                break

            used_model = model
            full_reply = ""

            try:
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "POST",
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
                            "max_tokens": 2048,
                            "temperature": 0.7,
                            "stream": True,
                        },
                        timeout=timeout,
                    ) as resp:
                        if resp.status_code == 401:
                            yield f"data: {json.dumps({'error': 'Credencial de IA invalida. Contate o suporte.'})}\n\n"
                            return

                        if resp.status_code == 429:
                            logger.warning("IA stream rate-limited model=%s", model)
                            continue

                        if resp.status_code == 402:
                            logger.warning("IA stream model no longer free model=%s (payment required)", model)
                            continue

                        if resp.status_code != 200:
                            body_preview = ""
                            async for _c in resp.aiter_text():
                                body_preview += _c
                                if len(body_preview) > 300:
                                    break
                            logger.warning(
                                "IA stream non-200 model=%s status=%d body=%s",
                                model, resp.status_code, body_preview[:300],
                            )
                            continue

                        # -- Parse SSE from OpenRouter --
                        phase = "buffering"   # buffering | thinking | streaming
                        pre_buffer = ""
                        sse_buf = ""
                        done_signal = False

                        async for chunk in resp.aiter_text():
                            sse_buf += chunk
                            while "\n" in sse_buf:
                                line, sse_buf = sse_buf.split("\n", 1)
                                line = line.strip()
                                if not line or not line.startswith("data: "):
                                    continue
                                payload = line[6:]
                                if payload == "[DONE]":
                                    done_signal = True
                                    break

                                try:
                                    data = json.loads(payload)
                                    delta = data["choices"][0]["delta"]
                                    token = delta.get("content", "")
                                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                                    continue
                                if not token:
                                    continue

                                # -- Thinking-token state machine --
                                if phase == "buffering":
                                    pre_buffer += token
                                    stripped = pre_buffer.lstrip()
                                    if "<think>" in stripped:
                                        phase = "thinking"
                                    elif stripped and stripped[0] != "<":
                                        phase = "streaming"
                                        full_reply = pre_buffer
                                        yield f"data: {json.dumps({'t': pre_buffer})}\n\n"
                                    elif len(stripped) >= 7 and not stripped.startswith("<think"):
                                        phase = "streaming"
                                        full_reply = pre_buffer
                                        yield f"data: {json.dumps({'t': pre_buffer})}\n\n"
                                elif phase == "thinking":
                                    pre_buffer += token
                                    if "</think>" in pre_buffer:
                                        idx = pre_buffer.index("</think>") + 8
                                        remainder = pre_buffer[idx:].lstrip()
                                        phase = "streaming"
                                        full_reply = remainder
                                        if remainder:
                                            yield f"data: {json.dumps({'t': remainder})}\n\n"
                                elif phase == "streaming":
                                    full_reply += token
                                    yield f"data: {json.dumps({'t': token})}\n\n"

                            if done_signal:
                                break

                        # Flush buffer for very short responses that never left buffering
                        if phase == "buffering" and pre_buffer.strip():
                            cleaned = _THINKING_RE.sub("", pre_buffer).strip()
                            if cleaned:
                                full_reply = cleaned
                                yield f"data: {json.dumps({'t': cleaned})}\n\n"
                        elif phase == "thinking":
                            cleaned = _THINKING_RE.sub("", pre_buffer).strip()
                            if cleaned:
                                full_reply = cleaned
                                yield f"data: {json.dumps({'t': cleaned})}\n\n"

                if full_reply.strip():
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    try:
                        log_interaction(
                            db=db,
                            user_id=current_user.id,
                            question=last_question,
                            snapshot_dict=snapshot.model_dump(),
                            response=full_reply,
                            model=used_model,
                        )
                    except Exception:
                        logger.warning("Failed to log streaming interaction", exc_info=True)
                    return

            except httpx.TimeoutException:
                logger.warning("IA stream timeout model=%s (%.0fs)", model, timeout)
                continue
            except httpx.RequestError as exc:
                logger.warning("IA stream conn error model=%s err=%s", model, exc)
                continue

        # All models exhausted
        yield f"data: {json.dumps({'error': 'Servico de IA temporariamente indisponivel. Tente novamente.'})}\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
