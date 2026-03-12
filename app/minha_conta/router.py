"""
User area routes — profile, financial health, subscription management.
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.cards.models import CreditCard
from app.core.database import get_db
from app.minha_conta import service as sub_service
from app.minha_conta.models import SubscriptionStatus

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

router = APIRouter()


@router.get("/ui/minha-conta", response_class=HTMLResponse)
async def minha_conta_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    subscription = sub_service.get_subscription(db, current_user.id)
    health = sub_service.get_financial_health(db, current_user)
    cards = (
        db.query(CreditCard)
        .filter(CreditCard.user_id == current_user.id, CreditCard.is_blocked == False)  # noqa: E712
        .all()
    )
    is_subscribed = subscription.status == SubscriptionStatus.ACTIVE

    return templates.TemplateResponse(
        "minha_conta.html",
        {
            "request": request,
            "page": "minha_conta",
            "user_name": current_user.name,
            "user": current_user,
            "health": health,
            "subscription": subscription,
            "is_subscribed": is_subscribed,
            "cards": cards,
        },
    )


class SubscribeCardRequest(BaseModel):
    card_id: str


@router.post("/minha-conta/assinar/saldo")
async def subscribe_balance(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return sub_service.subscribe_with_balance(db, current_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/minha-conta/assinar/cartao")
async def subscribe_card(
    payload: SubscribeCardRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return sub_service.subscribe_with_card(db, current_user, payload.card_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/minha-conta/saude-financeira")
async def financial_health_api(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return sub_service.get_financial_health(db, current_user)
