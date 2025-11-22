from sqlalchemy.orm import Session
from app.cards.models import CreditCard, CardType
from app.cards.schemas import CardCreateRequest
from app.auth.models import User
from uuid import uuid4
from datetime import datetime, timedelta
import random

def generate_card_number():
    """Generates a valid-looking Visa card number (starts with 4)."""
    # Prefix for Visa
    prefix = "4"
    # Generate 15 random digits
    random_digits = "".join([str(random.randint(0, 9)) for _ in range(15)])
    return prefix + random_digits

def generate_cvv():
    return "".join([str(random.randint(0, 9)) for _ in range(3)])

def generate_expiration_date(years=4):
    exp = datetime.now() + timedelta(days=365 * years)
    return exp.strftime("%m/%y")

def create_card(db: Session, user: User, data: CardCreateRequest) -> CreditCard:

    expires_at = None
    if data.type == CardType.VIRTUAL_TEMP:
        expires_at = datetime.utcnow() + timedelta(hours=24)

    # Default limit logic:
    # For MVP, we can set a default limit or share user's limit.
    # Let's set a default of 1000.0 for virtual cards if not specified (though schema doesn't allow specifying yet)
    default_limit = 1000.0

    card = CreditCard(
        id=str(uuid4()),
        user_id=user.id,
        card_number=generate_card_number(),
        cvv=generate_cvv(),
        expiration_date=generate_expiration_date(),
        card_holder_name=user.name.upper(),
        type=data.type,
        is_blocked=False,
        limit=default_limit,
        expires_at=expires_at
    )

    db.add(card)
    db.commit()
    db.refresh(card)
    return card

from sqlalchemy import or_

def list_cards(db: Session, user_id: str):
    # Filter out expired cards (where expires_at is in the past)
    # Keep cards where expires_at is NULL (permanent) or expires_at > now
    now = datetime.utcnow()
    return db.query(CreditCard).filter(
        CreditCard.user_id == user_id,
        or_(CreditCard.expires_at == None, CreditCard.expires_at > now)
    ).all()

def get_card(db: Session, card_id: str, user_id: str) -> CreditCard:
    now = datetime.utcnow()
    return db.query(CreditCard).filter(
        CreditCard.id == card_id,
        CreditCard.user_id == user_id,
        or_(CreditCard.expires_at == None, CreditCard.expires_at > now)
    ).first()

def delete_card(db: Session, card_id: str, user_id: str):
    card = get_card(db, card_id, user_id)
    if card:
        db.delete(card)
        db.commit()
    return card

def toggle_block_card(db: Session, card_id: str, user_id: str):
    card = get_card(db, card_id, user_id)
    if card:
        card.is_blocked = not card.is_blocked
        db.commit()
        db.refresh(card)
    return card

def update_card_limit(db: Session, card_id: str, user_id: str, new_limit: float):
    card = get_card(db, card_id, user_id)
    if card:
        card.limit = new_limit
        db.commit()
        db.refresh(card)
    return card
