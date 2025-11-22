from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.cards.schemas import CardCreateRequest, CardResponse, CardUpdateLimitRequest
from app.cards.service import create_card, list_cards, delete_card, toggle_block_card, update_card_limit

router = APIRouter()

@router.post("/", response_model=CardResponse, status_code=201)
def create_new_card(
    data: CardCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return create_card(db, current_user, data)

@router.get("/", response_model=List[CardResponse])
def get_my_cards(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return list_cards(db, current_user.id)

@router.delete("/{card_id}", status_code=204)
def remove_card(
    card_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    card = delete_card(db, card_id, current_user.id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return

@router.post("/{card_id}/block", response_model=CardResponse)
def block_unblock_card(
    card_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    card = toggle_block_card(db, card_id, current_user.id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card

@router.patch("/{card_id}/limit", response_model=CardResponse)
def adjust_limit(
    card_id: str,
    data: CardUpdateLimitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    card = update_card_limit(db, card_id, current_user.id, data.limit)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card
