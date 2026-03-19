"""
Subscription service — handles the R$9.90/month Bio Tech I.A account manager plan.
Covers balance payment, credit card payment, expiry check and financial health metrics.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.models import User
from app.boleto.models import BoletoTransaction, BoletoStatus
from app.cards.models import CreditCard
from app.core.logger import audit_log
from app.minha_conta.models import UserSubscription, SubscriptionStatus, PaymentMethod
from app.pix.models import PixTransaction, PixStatus, TransactionType

SUBSCRIPTION_AMOUNT: float = 9.90
SUBSCRIPTION_DAYS: int = 30


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_or_create(db: Session, user_id: str) -> UserSubscription:
    sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
    if not sub:
        sub = UserSubscription(user_id=user_id)
        db.add(sub)
        db.commit()
        db.refresh(sub)
    return sub


def _check_expiry(db: Session, sub: UserSubscription) -> UserSubscription:
    """Mark subscription as EXPIRED when the expiry date has passed.
    If auto_renew is enabled, attempt to deduct balance and renew automatically."""
    if sub.status == SubscriptionStatus.ACTIVE and sub.expires_at:
        now = datetime.now(timezone.utc)
        exp = sub.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < now:
            if sub.auto_renew:
                user = db.query(User).filter(User.id == sub.user_id).first()
                if user and user.balance >= SUBSCRIPTION_AMOUNT:
                    user.balance -= SUBSCRIPTION_AMOUNT
                    expires = now + timedelta(days=SUBSCRIPTION_DAYS)
                    sub.expires_at = expires
                    sub.last_renewed_at = now
                    db.commit()
                    db.refresh(sub)
                    db.refresh(user)
                    audit_log(
                        action="SUBSCRIPTION_AUTO_RENEWED",
                        user=sub.user_id,
                        resource=f"subscription_id={sub.id}",
                        details={
                            "amount": SUBSCRIPTION_AMOUNT,
                            "new_balance": user.balance,
                            "expires_at": expires.isoformat(),
                        },
                    )
                    return sub
            sub.status = SubscriptionStatus.EXPIRED
            db.commit()
            db.refresh(sub)
    return sub


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_subscription(db: Session, user_id: str) -> UserSubscription:
    sub = _get_or_create(db, user_id)
    return _check_expiry(db, sub)


def subscribe_with_balance(db: Session, user: User) -> Dict[str, Any]:
    """Deduct R$9.90 from the user balance and activate the plan."""
    if user.balance < SUBSCRIPTION_AMOUNT:
        raise ValueError(
            f"Saldo insuficiente. Necessário R$ {SUBSCRIPTION_AMOUNT:.2f}, "
            f"disponível R$ {user.balance:.2f}."
        )

    sub = _get_or_create(db, user.id)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SUBSCRIPTION_DAYS)

    user.balance -= SUBSCRIPTION_AMOUNT
    sub.status = SubscriptionStatus.ACTIVE
    sub.payment_method = PaymentMethod.BALANCE.value
    sub.card_id = None
    sub.plan_amount = SUBSCRIPTION_AMOUNT
    sub.subscribed_at = now
    sub.expires_at = expires
    sub.last_renewed_at = now
    sub.auto_renew = True

    db.commit()
    db.refresh(sub)
    db.refresh(user)

    audit_log(
        action="SUBSCRIPTION_ACTIVATED_BALANCE",
        user=user.id,
        resource=f"subscription_id={sub.id}",
        details={
            "amount": SUBSCRIPTION_AMOUNT,
            "payment_method": "SALDO",
            "new_balance": user.balance,
            "expires_at": expires.isoformat(),
        },
    )

    return {
        "ok": True,
        "expires_at": expires.isoformat(),
        "new_balance": user.balance,
    }


def subscribe_with_card(db: Session, user: User, card_id: str) -> Dict[str, Any]:
    """Charge the credit card and activate the plan (simulated)."""
    card = (
        db.query(CreditCard)
        .filter(CreditCard.id == card_id, CreditCard.user_id == user.id)
        .first()
    )
    if not card:
        raise ValueError("Cartão não encontrado.")
    if card.is_blocked:
        raise ValueError("Cartão bloqueado. Escolha outro cartão.")

    sub = _get_or_create(db, user.id)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SUBSCRIPTION_DAYS)

    sub.status = SubscriptionStatus.ACTIVE
    sub.payment_method = PaymentMethod.CREDIT_CARD.value
    sub.card_id = card_id
    sub.plan_amount = SUBSCRIPTION_AMOUNT
    sub.subscribed_at = now
    sub.expires_at = expires
    sub.last_renewed_at = now
    sub.auto_renew = True

    db.commit()
    db.refresh(sub)

    audit_log(
        action="SUBSCRIPTION_ACTIVATED_CARD",
        user=user.id,
        resource=f"subscription_id={sub.id}",
        details={
            "amount": SUBSCRIPTION_AMOUNT,
            "payment_method": "CARTAO",
            "card_id": card_id,
            "expires_at": expires.isoformat(),
        },
    )

    return {"ok": True, "expires_at": expires.isoformat()}


def cancel_subscription(db: Session, user: User) -> Dict[str, Any]:
    """Cancel auto-renewal. Subscription stays active until expires_at."""
    sub = _get_or_create(db, user.id)
    if sub.status != SubscriptionStatus.ACTIVE:
        raise ValueError("Nenhuma assinatura ativa para cancelar.")
    sub.auto_renew = False
    db.commit()
    db.refresh(sub)
    audit_log(
        action="SUBSCRIPTION_CANCELLED",
        user=user.id,
        resource=f"subscription_id={sub.id}",
        details={"expires_at": sub.expires_at.isoformat() if sub.expires_at else None},
    )
    return {
        "ok": True,
        "message": "Renovacao automatica cancelada. Seu plano continua ativo ate o vencimento.",
        "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
    }


TRIAL_HOURS: int = 24


def activate_trial(db: Session, user: User) -> Dict[str, Any]:
    """Grant a one-time 24-hour free trial. Can only be used once per user."""
    sub = _get_or_create(db, user.id)
    if sub.trial_used:
        raise ValueError("Periodo de teste ja utilizado. Assine o plano para continuar.")
    if sub.status == SubscriptionStatus.ACTIVE:
        raise ValueError("Voce ja possui uma assinatura ativa.")

    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=TRIAL_HOURS)

    sub.status = SubscriptionStatus.ACTIVE
    sub.payment_method = "TRIAL"
    sub.card_id = None
    sub.plan_amount = 0.0
    sub.subscribed_at = now
    sub.expires_at = expires
    sub.last_renewed_at = None
    sub.auto_renew = False
    sub.trial_used = True

    db.commit()
    db.refresh(sub)

    audit_log(
        action="SUBSCRIPTION_TRIAL_ACTIVATED",
        user=user.id,
        resource=f"subscription_id={sub.id}",
        details={"expires_at": expires.isoformat()},
    )

    return {"ok": True, "expires_at": expires.isoformat()}


def admin_grant_subscription(db: Session, target_user_id: str, admin_user_id: str) -> Dict[str, Any]:
    """Admin grants a 30-day subscription to any user at no cost."""
    sub = _get_or_create(db, target_user_id)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SUBSCRIPTION_DAYS)

    sub.status = SubscriptionStatus.ACTIVE
    sub.payment_method = "ADMIN_GRANT"
    sub.card_id = None
    sub.plan_amount = 0.0
    sub.subscribed_at = now
    sub.expires_at = expires
    sub.last_renewed_at = now
    sub.auto_renew = False

    db.commit()
    db.refresh(sub)

    audit_log(
        action="SUBSCRIPTION_ADMIN_GRANTED",
        user=admin_user_id,
        resource=f"subscription_id={sub.id}",
        details={
            "target_user_id": target_user_id,
            "expires_at": expires.isoformat(),
        },
    )

    return {"ok": True, "user_id": target_user_id, "expires_at": expires.isoformat()}


def get_financial_health(db: Session, user: User) -> Dict[str, Any]:
    """
    Compute financial health metrics and a composite 0-100 score.

    Score composition:
    - Balance > 0:     +20
    - Balance > 500:   +10
    - Balance > 2000:  +10
    - Received > Sent: +20
    - Active (>= 5 tx): +15
    - Active (>= 20 tx): +10 additional
    - Email verified:  +5
    - Doc verified:    +10
    """
    pix_received: float = (
        db.query(func.sum(PixTransaction.value))
        .filter(
            PixTransaction.user_id == user.id,
            PixTransaction.type == TransactionType.RECEIVED,
            PixTransaction.status == PixStatus.CONFIRMED,
        )
        .scalar()
        or 0.0
    )
    pix_sent: float = (
        db.query(func.sum(PixTransaction.value))
        .filter(
            PixTransaction.user_id == user.id,
            PixTransaction.type == TransactionType.SENT,
            PixTransaction.status == PixStatus.CONFIRMED,
        )
        .scalar()
        or 0.0
    )
    boleto_paid: float = (
        db.query(func.sum(BoletoTransaction.value))
        .filter(
            BoletoTransaction.user_id == user.id,
            BoletoTransaction.status == BoletoStatus.PAID,
        )
        .scalar()
        or 0.0
    )
    card_count: int = (
        db.query(CreditCard)
        .filter(CreditCard.user_id == user.id, CreditCard.is_blocked == False)  # noqa: E712
        .count()
    )
    total_tx: int = (
        db.query(PixTransaction).filter(PixTransaction.user_id == user.id).count()
    )

    # Composite score
    score = 0
    if user.balance > 0:
        score += 20
    if user.balance > 500:
        score += 10
    if user.balance > 2000:
        score += 10
    if pix_received > pix_sent:
        score += 20
    if total_tx >= 5:
        score += 15
    if total_tx >= 20:
        score += 10
    if user.email_verified:
        score += 5
    if user.document_verified:
        score += 10
    score = min(score, 100)

    if score >= 90:
        label, color = "Excelente", "green"
    elif score >= 70:
        label, color = "Bom", "blue"
    elif score >= 50:
        label, color = "Regular", "yellow"
    else:
        label, color = "Atencao", "red"

    return {
        "balance": user.balance,
        "total_received": pix_received,
        "total_sent": pix_sent,
        "boleto_paid": boleto_paid,
        "card_count": card_count,
        "total_transactions": total_tx,
        "health_score": score,
        "health_label": label,
        "health_color": color,
    }
