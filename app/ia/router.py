"""
BIO TECH PAY I.A — Financial intelligence agent endpoint.
Proxies user messages to OpenRouter (OpenAI-compatible API) with full persona injection.
Uses httpx (already a project dependency) — no extra SDK required.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List
import httpx

from app.core.config import settings
from app.auth.dependencies import get_current_user

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

You must behave as:
- financial mentor
- trusted advisor
- strategic planner
- friendly companion

Your communication style must be:
- human
- intelligent
- clear
- calm
- natural

You explain complex financial topics in simple language.

You help the user:
- monitor income
- monitor expenses
- build financial discipline
- analyze investments
- grow wealth

You support global markets including:
stocks, bonds, ETFs, real estate, commodities, crypto, agriculture, business investments.

You never guarantee profits.
You always explain risk.
You support communication in any language the user prefers.

Your official name is BIO TECH PAY I.A. You are available 24 hours a day, 7 days a week, always calm, intelligent and helpful.

Always respond in the same language the user writes in. Default to Brazilian Portuguese if uncertain.

Keep responses concise but complete — avoid walls of text. Use short paragraphs and bullet points when helpful for clarity.
"""


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]


@router.post("/chat")
async def ia_chat(
    request: ChatRequest,
    current_user=Depends(get_current_user),
):
    """
    Proxies conversation to OpenRouter with full persona injection.
    Requires OPENROUTER_API_KEY in environment. Returns 503 if key is absent.
    """
    if not settings.OPENROUTER_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Servico de IA temporariamente indisponivel. Configure OPENROUTER_API_KEY.",
        )

    # Build messages list: system prompt + last 20 turns (prevents context bloat)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for m in request.messages[-20:]:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content[:4000]})

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://new-credit-fintech.onrender.com",
                    "X-Title": "Bio Code Tech Pay",
                },
                json={
                    "model": "openai/gpt-4o-mini",
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
    return {"reply": reply}
