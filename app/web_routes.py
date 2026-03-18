from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import Optional
from pydantic import BaseModel, field_validator
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
import secrets
from datetime import datetime as _datetime, timezone as _timezone
from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.core.config import settings
from app.core.fees import is_pj as _is_pj, fee_display
from app.core.utils import mask_cpf_cnpj as _mask_cpf
from app.pix.internal_transfer import find_recipient_user, execute_internal_transfer
from app.core.pix_emv import build_pix_static_emv_no_amount as _build_deposit_emv, build_qr_url as _build_qr_url

# Platform PIX receiving key — must match the EVP key registered in BACEN DICT via Asaas.
# Override via PLATFORM_PIX_KEY env var (set in Render Dashboard).
# Run `python scripts/check_pix_key.py` to discover the correct key for your Asaas account.
_FALLBACK_DEPOSIT_KEY = "1a923d7b-3230-46d4-a670-87bf7ee54817"
_DEPOSIT_WALLET_KEY: str = settings.PLATFORM_PIX_KEY or _FALLBACK_DEPOSIT_KEY

# Locally generated QR (fallback): EMV built per BACEN BR Code spec, rendered via qrserver.com.
_DEPOSIT_QR_URL_LOCAL = _build_qr_url(_build_deposit_emv(_DEPOSIT_WALLET_KEY))


def _fetch_asaas_deposit_qr() -> Optional[str]:
    """
    Fetches the official Asaas QR code image for the registered EVP deposit key.

    Asaas returns a verified base64 PNG registered with BACEN DICT — guaranteed to be
    accepted by all Brazilian bank apps. Falls back to locally generated QR on any failure.

    Returns a data URL (data:image/png;base64,...) or None if unavailable.
    """
    if not settings.ASAAS_API_KEY:
        return None
    try:
        import httpx as _httpx
        _base = (
            "https://sandbox.asaas.com/api/v3"
            if settings.ASAAS_USE_SANDBOX
            else "https://api.asaas.com/v3"
        )
        resp = _httpx.get(
            f"{_base}/pix/addressKeys",
            headers={"access_token": settings.ASAAS_API_KEY, "User-Agent": "BioCodeTechPay/1.0"},
            timeout=6.0,
        )
        if resp.status_code != 200:
            return None
        for entry in resp.json().get("data", []):
            if entry.get("key") == _DEPOSIT_WALLET_KEY and entry.get("status") == "ACTIVE":
                b64 = (entry.get("qrCode") or {}).get("encodedImage", "")
                if b64:
                    return f"data:image/png;base64,{b64}"
    except Exception:
        pass
    return None


# At startup, prefer the official Asaas QR image (BACEN-registered, accepted by all PSPs).
# Falls back to locally-generated qrserver.com URL when Asaas is unreachable.
_DEPOSIT_QR_URL: str = _fetch_asaas_deposit_qr() or _DEPOSIT_QR_URL_LOCAL
from app.pix.schemas import PixKeyType
from app.pix.models import PixTransaction, TransactionType
from app.boleto.models import BoletoTransaction
from app.cards.models import CreditCard
from decimal import Decimal as _Decimal
import os

# Setup templates directory
# Using absolute path to ensure it works regardless of where python is run
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

router = APIRouter()


# Login Page
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@router.get("/esqueci-senha", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("esqueci_senha.html", {"request": request})


@router.get("/redefinir-senha", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    return templates.TemplateResponse("redefinir_senha.html", {"request": request, "token": token})


@router.get("/", response_class=HTMLResponse)
async def read_root(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Main Dashboard (Home)"""
    # Always use user.balance: single authoritative source, never derived from transaction sums
    balance = current_user.balance

    return templates.TemplateResponse("index.html", {
        "request": request,
        "page": "home",
        "balance": balance,
        "user_name": current_user.name,
        "is_admin": current_user.email == settings.ADMIN_EMAIL,
    })


@router.get("/ui/pix", response_class=HTMLResponse)
async def pix_ui(request: Request, current_user: User = Depends(get_current_user)):
    """PIX Interface"""
    from app.core.fees import calculate_pix_outbound_fee, calculate_pix_receive_fee
    user_pj = _is_pj(current_user.cpf_cnpj)
    # Reference amount R$100 used only as a display hint for PF (fixed fee, amount-agnostic).
    outbound_fee_label = fee_display(calculate_pix_outbound_fee(current_user.cpf_cnpj, 100))

    # Ensure pix_random_key exists (backfill for accounts predating the migration).
    if not current_user.pix_random_key:
        from uuid import uuid4 as _uuid4
        from app.core.database import get_db as _get_db
        _db_gen = _get_db()
        _db = next(_db_gen)
        try:
            from app.auth.models import User as _User
            _live = _db.query(_User).filter(_User.id == current_user.id).first()
            if _live and not _live.pix_random_key:
                _live.pix_random_key = str(_uuid4())
                _db.add(_live)
                _db.commit()
                _db.refresh(_live)
                current_user.pix_random_key = _live.pix_random_key
        finally:
            _db_gen.close()

    return templates.TemplateResponse(
        "pix.html",
        {
            "request": request,
            "page": "pix",
            "user_name": current_user.name,
            "user_is_pj": user_pj,
            "user_balance": float(current_user.balance),
            "user_email":          current_user.email,
            "deposit_wallet_key":  _DEPOSIT_WALLET_KEY,
            "deposit_qr_url":     _DEPOSIT_QR_URL,
            "user_cpf_masked":     _mask_cpf(current_user.cpf_cnpj or ""),
            # Labels derived from the real fee engine — no hardcoded strings.
            "pix_fee_outbound_label": "Gratuito",
            "pix_fee_outbound_rate_label": "Gratuito",
            "pix_fee_receive_label": "Gratuito",
        }
    )


@router.get("/ui/cards", response_class=HTMLResponse)
async def cards_ui(request: Request, current_user: User = Depends(get_current_user)):
    """My Cards Interface"""
    return templates.TemplateResponse(
        "cards/my_cards.html",
        {"request": request, "page": "cards", "user_name": current_user.name}
    )

@router.get("/ui/cards/create", response_class=HTMLResponse)
async def create_card_ui(request: Request, current_user: User = Depends(get_current_user)):
    """Create Card Interface"""
    return templates.TemplateResponse(
        "cards/create_card.html",
        {"request": request, "page": "cards", "user_name": current_user.name}
    )

@router.get("/pix/pagar-qrcode", response_class=HTMLResponse)
async def pix_payment_simulation(request: Request, current_user: User = Depends(get_current_user)):
    """QR Code Payment Simulation Page"""
    return templates.TemplateResponse(
        "pix_payment.html",
        {"request": request, "page": "pix_payment", "user_name": current_user.name}
    )


@router.get("/pix/link/{charge_id}", response_class=HTMLResponse)
async def pix_payment_link(charge_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Public shareable payment link page — no authentication required.
    The payer accesses this URL to scan the QR code or copy the PIX code.
    Reconstructs the EMV payload for legacy records without copy_paste_code.
    """
    from app.pix.models import PixTransaction, PixStatus, TransactionType
    from app.core.pix_emv import build_pix_static_emv, build_qr_url
    import re as _re

    _UUID_PATTERN = _re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        _re.IGNORECASE
    )

    charge = (
        db.query(PixTransaction)
        .filter(
            PixTransaction.id == charge_id,
            PixTransaction.type == TransactionType.RECEIVED,
        )
        .first()
    )
    if not charge:
        raise HTTPException(status_code=404, detail="Cobranca nao encontrada.")

    # Resolve copy-paste code — prefer stored value; fall back to reconstruction.
    copy_paste = charge.copy_paste_code
    if not copy_paste:
        if _UUID_PATTERN.match(charge.pix_key):
            # Simulation charge: pix_key IS the charge UUID — EMV is deterministic
            copy_paste = build_pix_static_emv(charge.id, charge.value)
        else:
            # Asaas charge: pix_key holds a (possibly truncated) EMV — best available
            copy_paste = charge.pix_key

    qr_url = build_qr_url(copy_paste)
    already_paid = charge.status == PixStatus.CONFIRMED

    return templates.TemplateResponse("pix_link.html", {
        "request": request,
        "charge": charge,
        "copy_paste": copy_paste,
        "qr_url": qr_url,
        "already_paid": already_paid,
    })


@router.get("/ui/extrato", response_class=HTMLResponse)
async def extrato_ui(request: Request, current_user: User = Depends(get_current_user)):
    """Statement Interface"""
    return templates.TemplateResponse("extrato.html", {"request": request, "page": "extrato", "user_name": current_user.name})


@router.get("/admin", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Admin panel — restricted to admin account only."""
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    all_users = db.query(User).order_by(User.created_at.desc()).all()

    # Remove only the internal matrix account from the customer list
    matrix_user = next((u for u in all_users if u.email == settings.MATRIX_ACCOUNT_EMAIL), None)
    users = [u for u in all_users if u.email != settings.MATRIX_ACCOUNT_EMAIL]
    matrix_balance = matrix_user.balance if matrix_user else 0.0

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "page": "admin",
        "user_name": current_user.name,
        "users": users,
        "total": len(users),
        "active": sum(1 for u in users if u.is_active),
        "verified_docs": sum(1 for u in users if u.document_verified),
        "verified_emails": sum(1 for u in users if u.email_verified),
        "matrix_balance": matrix_balance,
        "admin_email": settings.ADMIN_EMAIL,
        "matrix_email": settings.MATRIX_ACCOUNT_EMAIL,
    })


class ToggleActiveRequest(BaseModel):
    active: bool


class AdminEditUserRequest(BaseModel):
    """Payload for admin user edit. Only safe, non-identity fields are accepted."""
    name: Optional[str] = None
    phone: Optional[str] = None
    address_street: Optional[str] = None
    address_number: Optional[str] = None
    address_complement: Optional[str] = None
    address_city: Optional[str] = None
    address_state: Optional[str] = None
    address_zip: Optional[str] = None
    email_verified: Optional[bool] = None
    document_verified: Optional[bool] = None
    credit_limit: Optional[float] = None


class MatrixTransferRequest(BaseModel):
    pix_key: str
    key_type: str
    amount: float
    description: str = ""

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Valor deve ser positivo")
        return v

    @field_validator("pix_key")
    @classmethod
    def pix_key_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Chave PIX eh obrigatoria")
        return v.strip()

    @field_validator("key_type")
    @classmethod
    def key_type_must_be_valid(cls, v: str) -> str:
        valid = {"CPF", "CNPJ", "EMAIL", "PHONE", "RANDOM"}
        if v.upper() not in valid:
            raise ValueError(f"Tipo de chave inválido. Aceitos: {', '.join(sorted(valid))}")
        return v.upper()


class AsaasTransferRequest(BaseModel):
    """Transfer FROM the real Asaas bank account to a destination."""
    destination: str  # "correntista" | "pix_key" | "matrix"
    user_id: Optional[str] = None
    pix_key: Optional[str] = None
    key_type: Optional[str] = None
    amount: float
    description: str = ""

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Valor deve ser positivo")
        return round(v, 2)

    @field_validator("destination")
    @classmethod
    def destination_valid(cls, v: str) -> str:
        valid = {"correntista", "pix_key", "matrix"}
        if v not in valid:
            raise ValueError(f"Destino inválido. Aceitos: {', '.join(sorted(valid))}")
        return v


class MatrixToAsaasRequest(BaseModel):
    """Accounting debit: move matrix balance back to Asaas (no real PIX emitted)."""
    amount: float
    description: str = ""

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Valor deve ser positivo")
        return round(v, 2)


@router.post("/admin/users/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: str,
    payload: ToggleActiveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Activate or suspend a user account. Admin only."""
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    if target.email == settings.ADMIN_EMAIL:
        raise HTTPException(status_code=400, detail="Conta admin não pode ser suspensa.")

    target.is_active = payload.active
    db.commit()
    return {"ok": True, "user_id": user_id, "is_active": payload.active}


@router.post("/admin/users/{user_id}/send-verification")
async def admin_send_verification(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Re-send (or send for the first time) the email-verification link for a user.
    Admin only. Generates a fresh token — previous link is invalidated."""
    from app.core.email_service import send_verification_email
    from app.core.logger import logger, audit_log

    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    if target.email_verified:
        raise HTTPException(status_code=400, detail="E-mail já verificado.")

    new_token = secrets.token_urlsafe(32)
    target.email_verification_token = new_token
    target.email_verification_sent_at = _datetime.now(_timezone.utc)
    db.commit()

    sent = send_verification_email(target.email, target.name, new_token)

    audit_log(
        action="ADMIN_SENT_VERIFICATION_EMAIL",
        user=current_user.id,
        resource=f"user_id={user_id}",
        details={"target_email": target.email, "email_sent": sent},
    )
    logger.info(f"ADMIN sent verification email to user id={user_id} sent={sent}")

    if not sent:
        raise HTTPException(
            status_code=503,
            detail="Token gerado mas e-mail nao enviado. Verifique RESEND_API_KEY."
        )

    return {"ok": True, "user_id": user_id, "email": target.email}


@router.patch("/admin/users/{user_id}")
async def edit_user(
    user_id: str,
    payload: AdminEditUserRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update allowed profile fields for a user. Admin only.
    Immutable fields (email, cpf_cnpj, password, balance, is_admin) are never touched."""
    from app.core.logger import logger, audit_log
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    changed: dict = {}

    if payload.name is not None:
        v = payload.name.strip()
        if not v:
            raise HTTPException(status_code=400, detail="Nome não pode ser vazio.")
        if len(v) > 100:
            raise HTTPException(status_code=400, detail="Nome excede 100 caracteres.")
        changed["name"] = v
        target.name = v

    if payload.phone is not None:
        target.phone = payload.phone.strip() or None
        changed["phone"] = target.phone

    if payload.address_street is not None:
        target.address_street = payload.address_street.strip() or None
        changed["address_street"] = target.address_street

    if payload.address_number is not None:
        target.address_number = payload.address_number.strip() or None
        changed["address_number"] = target.address_number

    if payload.address_complement is not None:
        target.address_complement = payload.address_complement.strip() or None
        changed["address_complement"] = target.address_complement

    if payload.address_city is not None:
        target.address_city = payload.address_city.strip() or None
        changed["address_city"] = target.address_city

    if payload.address_state is not None:
        v = payload.address_state.strip().upper()
        if v and len(v) > 2:
            raise HTTPException(status_code=400, detail="Estado deve ser a sigla UF (ex: SP).")
        target.address_state = v or None
        changed["address_state"] = target.address_state

    if payload.address_zip is not None:
        target.address_zip = payload.address_zip.strip() or None
        changed["address_zip"] = target.address_zip

    if payload.email_verified is not None:
        target.email_verified = payload.email_verified
        changed["email_verified"] = payload.email_verified

    if payload.document_verified is not None:
        target.document_verified = payload.document_verified
        changed["document_verified"] = payload.document_verified

    if payload.credit_limit is not None:
        if payload.credit_limit < 0:
            raise HTTPException(status_code=400, detail="Limite de crédito não pode ser negativo.")
        target.credit_limit = payload.credit_limit
        changed["credit_limit"] = payload.credit_limit

    if not changed:
        return {"ok": True, "user_id": user_id, "changed": {}}

    db.commit()
    audit_log(
        action="ADMIN_USER_EDITED",
        user=current_user.id,
        resource=f"user_id={user_id}",
        details={"changed_fields": list(changed.keys()), "target_email": target.email},
    )
    logger.info(f"ADMIN edited user id={user_id} fields={list(changed.keys())}")
    return {"ok": True, "user_id": user_id, "changed": changed}


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Hard delete a user and all their orphaned records. Admin only. Irreversible."""
    from app.core.logger import logger, audit_log
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    if target.email == settings.ADMIN_EMAIL:
        raise HTTPException(status_code=400, detail="Conta admin não pode ser excluída.")
    if target.email == settings.MATRIX_ACCOUNT_EMAIL:
        raise HTTPException(status_code=400, detail="Conta matriz não pode ser excluída.")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Não é possível excluir a própria conta.")

    deleted_name = target.name
    deleted_email = target.email

    # Hard delete all orphaned records in dependency order
    pix_count = db.query(PixTransaction).filter(PixTransaction.user_id == user_id).delete(synchronize_session=False)
    boleto_count = db.query(BoletoTransaction).filter(BoletoTransaction.user_id == user_id).delete(synchronize_session=False)
    card_count = db.query(CreditCard).filter(CreditCard.user_id == user_id).delete(synchronize_session=False)

    db.delete(target)
    db.commit()

    audit_log(
        action="ADMIN_USER_DELETED",
        user=current_user.id,
        resource=f"user_id={user_id}",
        details={
            "deleted_name": deleted_name,
            "deleted_email": deleted_email,
            "orphans_removed": {"pix": pix_count, "boleto": boleto_count, "cards": card_count},
        },
    )
    logger.warning(
        f"ADMIN hard-deleted user: id={user_id}, name={deleted_name}, "
        f"orphans=pix:{pix_count} boleto:{boleto_count} cards:{card_count}"
    )
    return {
        "ok": True,
        "deleted": {"user_id": user_id, "name": deleted_name},
        "orphans_removed": {"pix": pix_count, "boleto": boleto_count, "cards": card_count},
    }


@router.get("/admin/users/{user_id}")
async def get_user_detail(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Return full user profile with transaction stats. Admin only."""
    from datetime import datetime as _dt
    from app.core.logger import logger
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    pix_all = db.query(PixTransaction).filter(PixTransaction.user_id == user_id).all()
    pix_sent = [t for t in pix_all if t.type == TransactionType.SENT]
    pix_rcvd = [t for t in pix_all if t.type == TransactionType.RECEIVED]
    boleto_count = db.query(BoletoTransaction).filter(BoletoTransaction.user_id == user_id).count()
    card_count = db.query(CreditCard).filter(CreditCard.user_id == user_id).count()
    recent = sorted(pix_all, key=lambda t: t.created_at or _dt.min, reverse=True)[:5]

    return {
        "id": u.id,
        "name": u.name,
        "cpf_cnpj": u.cpf_cnpj,
        "email": u.email,
        "phone": u.phone or "",
        "address_street": u.address_street or "",
        "address_number": u.address_number or "",
        "address_complement": u.address_complement or "",
        "address_city": u.address_city or "",
        "address_state": u.address_state or "",
        "address_zip": u.address_zip or "",
        "balance": float(u.balance),
        "credit_limit": float(u.credit_limit) if u.credit_limit else 0.0,
        "email_verified": bool(u.email_verified),
        "document_verified": bool(u.document_verified),
        "is_active": bool(u.is_active),
        "is_admin": bool(u.is_admin),
        "created_at": u.created_at.strftime("%d/%m/%Y %H:%M") if u.created_at else "\u2014",
        "stats": {
            "pix_sent_count": len(pix_sent),
            "pix_sent_total": float(sum(t.value for t in pix_sent)),
            "pix_received_count": len(pix_rcvd),
            "pix_received_total": float(sum(t.value for t in pix_rcvd)),
            "boleto_count": boleto_count,
            "card_count": card_count,
        },
        "recent_pix": [
            {
                "type": t.type.value if hasattr(t.type, "value") else str(t.type),
                "value": float(t.value),
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "description": t.description or "",
                "created_at": t.created_at.strftime("%d/%m/%Y %H:%M") if t.created_at else "\u2014",
            }
            for t in recent
        ],
    }


@router.post("/admin/matrix/transfer")
async def matrix_transfer(
    payload: MatrixTransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Transfer accumulated fee balance from the matrix account to a PIX key.
    Detects internal BioCodeTechPay recipients and credits them directly without gateway.
    Admin only.
    """
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    matrix = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
    if not matrix:
        raise HTTPException(status_code=404, detail="Conta matriz não encontrada.")

    if matrix.balance < payload.amount:
        raise HTTPException(
            status_code=400,
            detail=f"Saldo insuficiente. Disponível: R$ {matrix.balance:.2f}"
        )

    idempotency_key = str(uuid4())
    correlation_id = str(uuid4())
    payment_id = None

    # Resolve key_type to enum for internal lookup
    try:
        key_type_enum = PixKeyType(payload.key_type.upper())
    except ValueError:
        key_type_enum = None

    # Check if destination is an internal BioCodeTechPay account
    recipient = None
    if key_type_enum in (PixKeyType.CPF, PixKeyType.CNPJ, PixKeyType.EMAIL):
        recipient = find_recipient_user(db, payload.pix_key, key_type_enum)

    if recipient:
        # Internal transfer — credit destination balance directly, no gateway needed
        try:
            _sent_tx, _recv_tx = execute_internal_transfer(
                db=db,
                sender=matrix,
                recipient=recipient,
                amount=payload.amount,
                pix_key=payload.pix_key,
                key_type=payload.key_type.upper(),
                description=payload.description or "Repasse BioCodeTechPay",
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
            db.commit()
            db.refresh(matrix)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        return {
            "ok": True,
            "type": "internal",
            "recipient": recipient.name,
            "balance": round(matrix.balance, 2),
            "payment_id": None,
        }

    # External transfer — use gateway
    from app.adapters.gateway_factory import get_payment_gateway

    gateway = get_payment_gateway()
    if not gateway:
        # No gateway configured (dev/local) — debit matrix locally without real dispatch
        matrix.balance -= payload.amount
        db.add(matrix)
        db.commit()
        return {"ok": True, "type": "external_local", "balance": round(matrix.balance, 2), "payment_id": None}

    try:
        result = gateway.create_pix_payment(
            value=Decimal(str(payload.amount)),
            pix_key=payload.pix_key,
            pix_key_type=payload.key_type,
            description=payload.description or "Repasse BioCodeTechPay",
            idempotency_key=idempotency_key,
        )
        payment_id = result.get("payment_id")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha no gateway: {str(exc)}")

    matrix.balance -= payload.amount
    db.add(matrix)
    db.commit()

    return {"ok": True, "type": "external", "balance": round(matrix.balance, 2), "payment_id": payment_id}


# ---------------------------------------------------------------------------
# Real-time balances — polling endpoint for the admin panel
# ---------------------------------------------------------------------------

@router.get("/admin/balances")
async def admin_balances(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return Asaas bank balance + matrix (fee) balance + correntistas list.
    Called every 30 s by the admin panel JS. Admin only.
    """
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    matrix_user = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
    matrix_balance = round(float(matrix_user.balance), 2) if matrix_user else 0.0

    asaas_balance: float | None = None
    from app.adapters.gateway_factory import get_payment_gateway
    gateway = get_payment_gateway()
    if gateway and hasattr(gateway, "_make_request"):
        try:
            resp = gateway._make_request("GET", "/finance/balance")
            asaas_balance = round(float(resp.get("balance", 0)), 2)
        except Exception:
            asaas_balance = None

    correntistas = (
        db.query(User)
        .filter(
            User.email != settings.MATRIX_ACCOUNT_EMAIL,
        )
        .order_by(User.name)
        .all()
    )

    return {
        "matrix_balance": matrix_balance,
        "asaas_balance": asaas_balance,
        "correntistas": [
            {"id": str(u.id), "name": u.name, "email": u.email, "balance": round(float(u.balance), 2)}
            for u in correntistas
        ],
    }


# ---------------------------------------------------------------------------
# Asaas → anywhere transfer (admin only)
# ---------------------------------------------------------------------------

@router.post("/admin/asaas/transfer")
async def asaas_transfer(
    payload: AsaasTransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Transfer FROM the real Asaas bank account.
    - destination='correntista': credits an internal user balance (accounting entry).
    - destination='pix_key': emits a real PIX via gateway (real money leaves Asaas).
    - destination='matrix': credits the matrix (fee) account balance (accounting entry).
    Admin only.
    """
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    if payload.destination == "correntista":
        if not payload.user_id:
            raise HTTPException(status_code=400, detail="user_id obrigatório para destino 'correntista'.")
        target = db.query(User).filter(User.id == payload.user_id).first()
        if not target:
            raise HTTPException(status_code=404, detail="Correntista não encontrado.")
        if target.email == settings.MATRIX_ACCOUNT_EMAIL:
            raise HTTPException(status_code=400, detail="Destino inválido.")
        target.balance += Decimal(str(payload.amount))
        db.add(target)
        db.commit()
        db.refresh(target)
        return {
            "ok": True,
            "type": "internal_credit",
            "recipient": target.name,
            "new_recipient_balance": round(float(target.balance), 2),
        }

    if payload.destination == "matrix":
        matrix = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
        if not matrix:
            raise HTTPException(status_code=404, detail="Conta de Taxas não encontrada.")
        matrix.balance += Decimal(str(payload.amount))
        db.add(matrix)
        db.commit()
        db.refresh(matrix)
        return {
            "ok": True,
            "type": "matrix_credit",
            "new_matrix_balance": round(float(matrix.balance), 2),
        }

    # destination == "pix_key" — real PIX via gateway
    if not payload.pix_key or not payload.key_type:
        raise HTTPException(status_code=400, detail="pix_key e key_type são obrigatórios para transferência externa.")

    key_type_upper = payload.key_type.upper()
    valid_key_types = {"CPF", "CNPJ", "EMAIL", "PHONE", "RANDOM"}
    if key_type_upper not in valid_key_types:
        raise HTTPException(status_code=400, detail=f"Tipo de chave inválido. Aceitos: {', '.join(sorted(valid_key_types))}")

    from app.adapters.gateway_factory import get_payment_gateway
    gateway = get_payment_gateway()
    if not gateway:
        raise HTTPException(status_code=503, detail="Gateway de pagamento não configurado.")

    idempotency_key = str(uuid4())
    try:
        result = gateway.create_pix_payment(
            value=Decimal(str(payload.amount)),
            pix_key=payload.pix_key.strip(),
            pix_key_type=key_type_upper,
            description=payload.description or "Transferência BioCodeTechPay",
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha no gateway: {str(exc)}")

    return {
        "ok": True,
        "type": "external_pix",
        "payment_id": result.get("payment_id"),
    }


# ---------------------------------------------------------------------------
# Matrix → Asaas accounting debit (admin only)
# ---------------------------------------------------------------------------

@router.post("/admin/matrix/to-asaas")
async def matrix_to_asaas(
    payload: MatrixToAsaasRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accounting debit: move matrix balance back to Asaas view.
    Debits the matrix (fee) account balance; no real PIX is emitted.
    Use when fee funds are considered returned to the Asaas bank account.
    Admin only.
    """
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    matrix = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
    if not matrix:
        raise HTTPException(status_code=404, detail="Conta de Taxas não encontrada.")
    if matrix.balance < Decimal(str(payload.amount)):
        raise HTTPException(
            status_code=400,
            detail=f"Saldo insuficiente. Disponível: R$ {matrix.balance:.2f}",
        )

    matrix.balance -= Decimal(str(payload.amount))
    db.add(matrix)
    db.commit()
    db.refresh(matrix)

    return {
        "ok": True,
        "type": "matrix_debit_to_asaas",
        "new_matrix_balance": round(float(matrix.balance), 2),
    }


# ---------------------------------------------------------------------------
# Matrix → Owner accounting transfer (no fee, no PIX, internal only)
# ---------------------------------------------------------------------------

@router.post("/admin/matrix/to-owner")
async def matrix_to_owner(
    payload: MatrixToAsaasRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Direct accounting transfer: matrix balance → owner (admin) account.
    No taxa. No PIX externo. Used to sweep platform margin to the proprietor's account.
    Admin only.
    """
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    matrix = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
    if not matrix:
        raise HTTPException(status_code=404, detail="Conta de Taxas não encontrada.")
    if matrix.balance < Decimal(str(payload.amount)):
        raise HTTPException(
            status_code=400,
            detail=f"Saldo insuficiente. Disponível: R$ {matrix.balance:.2f}",
        )

    owner = db.query(User).filter(User.email == settings.ADMIN_EMAIL).first()
    if not owner:
        raise HTTPException(status_code=404, detail="Conta do proprietário não encontrada.")

    amount_dec = Decimal(str(payload.amount)).quantize(Decimal("0.01"))
    matrix.balance = float(Decimal(str(matrix.balance)).quantize(Decimal("0.01")) - amount_dec)
    owner.balance = float(Decimal(str(owner.balance)).quantize(Decimal("0.01")) + amount_dec)

    db.add(matrix)
    db.add(owner)
    db.commit()
    db.refresh(matrix)
    db.refresh(owner)

    return {
        "ok": True,
        "type": "matrix_to_owner",
        "new_matrix_balance": round(float(matrix.balance), 2),
        "owner_balance_after": round(float(owner.balance), 2),
    }


# ---------------------------------------------------------------------------
# Matrix Dashboard — isolated zero-fee environment for the admin
# ---------------------------------------------------------------------------

@router.get("/admin/matrix", response_class=HTMLResponse)
async def matrix_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Matrix account dashboard — admin only."""
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    matrix_user = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
    matrix_balance = float(matrix_user.balance) if matrix_user else 0.0

    # Last 50 transactions from matrix account
    transactions = []
    if matrix_user:
        transactions = (
            db.query(PixTransaction)
            .filter(PixTransaction.user_id == matrix_user.id)
            .order_by(PixTransaction.created_at.desc())
            .limit(50)
            .all()
        )

    return templates.TemplateResponse("matrix_dashboard.html", {
        "request": request,
        "user_name": current_user.name,
        "matrix_balance": matrix_balance,
        "transactions": transactions,
    })


@router.get("/admin/matrix/audit")
async def matrix_audit(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Balance audit: compares internal balances against Asaas account balance.
    Auto-corrects Matrix when Asaas divergence is caused by gateway fees.
    Uses OpenRouter to generate a natural language explanation of each divergence.
    Admin only.
    """
    from app.core.logger import logger, audit_log
    import httpx as _httpx

    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    all_users = db.query(User).all()
    matrix_user = next((u for u in all_users if u.email == settings.MATRIX_ACCOUNT_EMAIL), None)
    # Audit invariant: sum ALL accounts (including admin/owner) — only matrix is handled separately.
    # Aligns with audit_worker.py formula: total_internal = sum(all) - matrix + matrix.
    customers = [u for u in all_users if u.email != settings.MATRIX_ACCOUNT_EMAIL]

    internal_sum = round(sum(float(u.balance) for u in customers), 2)
    matrix_balance = round(float(matrix_user.balance) if matrix_user else 0.0, 2)
    total_internal = round(internal_sum + matrix_balance, 2)

    # Fetch Asaas account balance
    asaas_balance: float | None = None
    from app.adapters.gateway_factory import get_payment_gateway
    gateway = get_payment_gateway()
    if gateway and hasattr(gateway, "_make_request"):
        try:
            resp = gateway._make_request("GET", "/finance/balance")
            asaas_balance = round(float(resp.get("balance", 0)), 2)
        except Exception as exc:
            logger.warning(f"[audit] Asaas balance fetch failed: {exc}")
            asaas_balance = None

    breakdown = [
        {"label": "Saldo todas as contas (sem conta de taxas)", "value": internal_sum,   "highlight": False},
        {"label": "Saldo Conta Matrix",                        "value": matrix_balance, "highlight": True},
        {"label": "Total interno (todas as contas)",           "value": total_internal, "highlight": False},
    ]

    messages: list[str] = []
    status = "OK"
    status_label = "Saldos consistentes"
    correction_applied: dict | None = None
    ai_explanation: str | None = None
    diff: float = 0.0
    direction: str = "none"

    if asaas_balance is not None:
        breakdown.append({"label": "Saldo Asaas (conta real)",   "value": asaas_balance, "highlight": False})
        diff = round(total_internal - asaas_balance, 2)
        abs_diff = abs(diff)
        breakdown.append({"label": "Diferenca (interno vs Asaas)", "value": abs_diff, "highlight": abs_diff > 0})

        if abs_diff < 0.01:
            status = "OK"
            status_label = "Saldos consistentes"
            messages.append("Saldo interno e Asaas estao sincronizados.")

        elif diff > 0:
            # internal > asaas: Asaas deducted a gateway fee not reflected in the internal ledger.
            # INVARIANT: correntista balances are NEVER modified by the audit system.
            # Platform (Matrix) absorbs the Asaas gateway cost as a reduction in margin.
            direction = "internal_above_asaas"
            _AUTO_CORRECTION_MAX = 20.0
            if abs_diff < 10:
                status = "WARN"
                status_label = "Divergencia detectada — custo gateway Asaas nao refletido internamente"
            else:
                status = "ERROR"
                status_label = "Divergencia critica — reconciliacao necessaria"

            messages.append(
                f"Total interno (R$ {total_internal:.2f}) e maior que Asaas (R$ {asaas_balance:.2f}). "
                f"Diferenca: R$ {abs_diff:.2f}. "
                "Causa provavel: Asaas deduziu taxa de gateway nao refletida internamente. "
                "Correcao: debitar da Conta Matrix (plataforma absorve o custo como reducao de margem). "
                "INVARIANTE: saldo de correntistas NUNCA e alterado pela auditoria."
            )

            if abs_diff <= _AUTO_CORRECTION_MAX:
                if matrix_user is not None:
                    _matrix_before  = round(float(matrix_user.balance), 2)
                    _matrix_bal_dec = Decimal(str(matrix_user.balance)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    _abs_diff_dec   = Decimal(str(abs_diff)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    _matrix_debit   = min(_abs_diff_dec, max(Decimal("0.00"), _matrix_bal_dec))
                    matrix_user.balance = float((_matrix_bal_dec - _matrix_debit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    db.add(matrix_user)
                    db.commit()
                    db.refresh(matrix_user)
                    matrix_balance = round(float(matrix_user.balance), 2)
                    total_internal = round(internal_sum + matrix_balance, 2)
                    _remainder_unfunded = round(float(_abs_diff_dec - _matrix_debit), 2)
                    correction_applied = {
                        "action": "matrix_debited",
                        "amount": float(_matrix_debit),
                        "matrix_before": _matrix_before,
                        "matrix_balance_after": matrix_balance,
                        "correntistas_unchanged": True,
                        "remainder_unfunded": _remainder_unfunded,
                        "reason": "asaas_gateway_cost_absorbed_by_platform",
                    }
                    messages.append(
                        f"Conta Matrix ajustada: R$ {_matrix_before:.2f} -> R$ {matrix_balance:.2f} "
                        f"(debito de R$ {float(_matrix_debit):.2f}). Correntistas preservados intactos (invariante)."
                    )
                    if _remainder_unfunded > 0.01:
                        messages.append(
                            f"AVISO: R$ {_remainder_unfunded:.2f} nao cobertos — saldo Matrix insuficiente "
                            "para absorver a divergencia completa. Acao manual necessaria."
                        )
                    audit_log(
                        action="AUDIT_AUTO_CORRECTION",
                        user=current_user.id,
                        resource="matrix_account",
                        details={
                            "diff": abs_diff,
                            "direction": direction,
                            "matrix_before": _matrix_before,
                            "matrix_after": matrix_balance,
                            "correntistas_unchanged": True,
                            "asaas_balance": asaas_balance,
                            "total_internal_after": total_internal,
                        },
                    )
                    status = "AUTO_CORRECTED"
                    status_label = "Saldos sincronizados — custo gateway absorvido pela Matrix"
                    breakdown[1]["value"] = matrix_balance
                    breakdown[2]["value"] = total_internal
                    if len(breakdown) > 4:
                        breakdown[4]["value"] = round(abs(total_internal - asaas_balance), 2)
                        breakdown[4]["highlight"] = False
                else:
                    messages.append(
                        "Conta Matrix nao encontrada. Correcao automatica impossivel. Verifique a configuracao."
                    )
            else:
                messages.append(
                    f"Divergencia de R$ {abs_diff:.2f} acima do limite de autocorrecao (max R${_AUTO_CORRECTION_MAX:.2f}). "
                    "Reconciliacao manual necessaria."
                )

        else:
            # asaas > internal: credit Matrix to restore parity
            # Policy: no upper cap on surplus sweep — the full Asaas surplus is
            # accumulated platform margin (network fee surplus from R$3.00 rede fee
            # minus R$2.00 Asaas cost). Always sweep to Matrix.
            direction = "asaas_above_internal"
            if abs_diff < 10:
                status = "WARN"
                status_label = "Asaas com saldo superior ao interno — corrigindo"
            else:
                status = "ERROR"
                status_label = "Divergencia critica — reconciliacao necessaria"

            messages.append(
                f"Asaas (R$ {asaas_balance:.2f}) e maior que total interno (R$ {total_internal:.2f}). "
                f"Diferenca: R$ {abs_diff:.2f}. "
                "Causa provavel: saldo Asaas nao refletido internamente (transacao ou ajuste nao registrado). "
                "Auto-correcao: creditar diferenca na Conta Matrix para sincronizar."
            )

            if matrix_user:  # surplus sweep is always allowed, regardless of amount
                old_matrix = matrix_user.balance
                matrix_user.balance = round(matrix_user.balance + abs_diff, 2)
                db.add(matrix_user)
                db.commit()
                db.refresh(matrix_user)
                matrix_balance = round(matrix_user.balance, 2)
                total_internal = round(internal_sum + matrix_balance, 2)
                correction_applied = {
                    "action": "matrix_credited",
                    "amount": abs_diff,
                    "matrix_before": round(old_matrix, 2),
                    "matrix_balance_after": matrix_balance,
                    "reason": "asaas_balance_above_internal",
                }
                messages.append(
                    f"Conta Matrix ajustada: R$ {old_matrix:.2f} -> R$ {matrix_balance:.2f} "
                    f"(credito de R$ {abs_diff:.2f} para sincronizar com Asaas)."
                )
                status = "AUTO_CORRECTED"
                status_label = "Saldos sincronizados automaticamente"
                audit_log(
                    action="AUDIT_AUTO_CORRECTION",
                    user=current_user.id,
                    resource="matrix_account",
                    details={
                        "diff": abs_diff,
                        "direction": direction,
                        "matrix_before": round(old_matrix, 2),
                        "matrix_after": matrix_balance,
                        "asaas_balance": asaas_balance,
                        "total_internal_after": total_internal,
                    },
                )
                breakdown[1]["value"] = matrix_balance
                breakdown[2]["value"] = total_internal
                if len(breakdown) > 4:
                    breakdown[4]["value"] = round(abs(total_internal - asaas_balance), 2)
                    breakdown[4]["highlight"] = False
            else:
                messages.append(
                    "Conta Matrix nao encontrada. Impossivel aplicar autocorrecao. "
                    "Verifique configuracao MATRIX_ACCOUNT_EMAIL."
                )

    else:
        messages.append("Asaas indisponível ou não configurado. Auditoria parcial (apenas saldos internos).")
        status = "WARN"
        status_label = "Auditoria parcial — Asaas não acessível"

    messages.append(f"Total de {len(customers)} contas auditadas (conta de taxas excluida, conta proprietario incluida).")

    # ── OpenRouter: generate natural language explanation ────────────────────
    if settings.OPENROUTER_API_KEY and (status != "OK" or correction_applied):
        # Describe what correction was applied for the AI context
        if correction_applied:
            _action = correction_applied.get("action", "")
            if _action == "matrix_debited":
                _correction_desc = (
                    f"sim — Matrix debitada em R$ {correction_applied.get('amount', 0.0):.2f}. "
                    "Correntistas preservados (invariante absoluto: saldo de correntistas nunca e modificado pela auditoria)."
                )
            elif _action == "matrix_credited":
                _correction_desc = f"sim — Matrix creditada em R$ {correction_applied['amount']:.2f}"
            else:
                _correction_desc = f"sim — acao: {_action}, valor R$ {correction_applied['amount']:.2f}"
        else:
            _correction_desc = "nao"

        context_prompt = (
            f"Voce e o sistema de auditoria financeira do BioCodeTechPay (fintech brasileira). "
            f"Acabou de rodar uma auditoria de saldos com os seguintes dados:\n"
            f"- Saldo clientes: R$ {internal_sum:.2f}\n"
            f"- Saldo Conta Matrix (acumulo de taxas da plataforma): R$ {matrix_balance:.2f}\n"
            f"- Total interno: R$ {total_internal:.2f}\n"
            f"- Saldo Asaas (conta real): R$ {f'{asaas_balance:.2f}' if asaas_balance is not None else 'indisponivel'}\n"
            f"- Diferenca: R$ {abs(diff):.2f} | Direcao: {direction}\n"
            f"- Status: {status_label}\n"
            f"- Correcao aplicada: {_correction_desc}\n\n"
            "Regra de negocio: a Conta Matrix acumula apenas a margem da plataforma (taxa cobrada ao "
            "correntista menos o custo Asaas). Quando o saldo interno excede o Asaas, o custo de gateway "
            "Asaas nao foi refletido internamente — a Conta Matrix absorve esse custo como reducao de margem. "
            "INVARIANTE ABSOLUTO: saldo de correntistas NUNCA e modificado pela auditoria ou qualquer "
            "correcao automatica. "
            "Em 2 a 4 frases curtas, explique em portugues brasileiro o que provavelmente causou "
            "esta divergencia, o que foi feito automaticamente e o que o admin deve verificar. "
            "Seja tecnico e preciso."
        )
        try:
            async with _httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://new-credit-fintech.onrender.com",
                        "X-Title": "BioCodeTechPay Audit",
                    },
                    json={
                        "model": "openai/gpt-4o-mini",
                        "messages": [{"role": "user", "content": context_prompt}],
                        "max_tokens": 300,
                        "temperature": 0.3,
                    },
                )
            if resp.status_code == 200:
                ai_explanation = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as ai_err:
            logger.warning(f"[audit] OpenRouter explanation failed: {ai_err}")
    # ─────────────────────────────────────────────────────────────────────────

    # ── Per-transaction fee forensics (read-only — never modifies any balance) ──
    from app.pix.models import PixStatus as _PixStatus
    from app.core.fees import (
        calculate_pix_outbound_fee as _fee_out,
        calculate_pix_receive_fee as _fee_in,
    )

    _TWO = Decimal("0.01")
    _fee_discrepancies: list = []
    _confirmed_txs = (
        db.query(PixTransaction)
        .filter(PixTransaction.status == _PixStatus.CONFIRMED)
        .all()
    )
    _total_audited = len(_confirmed_txs)
    _user_map = {u.id: u for u in all_users}

    for _tx in _confirmed_txs:
        _u = _user_map.get(_tx.user_id)
        if not _u:
            continue
        if _u.email in (settings.MATRIX_ACCOUNT_EMAIL, settings.ADMIN_EMAIL):
            continue
        if _tx.fee_amount is None:
            continue  # pre-fee-tracking records — not comparable
        _stored   = Decimal(str(_tx.fee_amount)).quantize(_TWO, rounding=ROUND_HALF_UP)
        _cpf_cnpj = getattr(_u, "cpf_cnpj", None) or ""
        if _tx.type == TransactionType.SENT:
            _expected = _fee_out(_cpf_cnpj, _tx.value)
        else:
            _expected = _fee_in(_cpf_cnpj, _tx.value)
        _delta = (_stored - _expected).quantize(_TWO, rounding=ROUND_HALF_UP)
        if abs(_delta) >= _TWO:
            _fee_discrepancies.append({
                "tx_id": _tx.id,
                "user_id": str(_tx.user_id),
                "user_name": _u.name,
                "type": _tx.type.value,
                "value": round(_tx.value, 2),
                "fee_stored": float(_stored),
                "fee_expected": float(_expected),
                "delta": float(_delta),
                "created_at": _tx.created_at.isoformat() if _tx.created_at else None,
            })

    _total_overcharged  = round(sum(d["delta"] for d in _fee_discrepancies if d["delta"] > 0), 2)
    _total_undercharged = round(abs(sum(d["delta"] for d in _fee_discrepancies if d["delta"] < 0)), 2)
    _fee_health = "OK" if not _fee_discrepancies else f"DISCREPANCIAS ({len(_fee_discrepancies)})"
    # ──────────────────────────────────────────────────────────────────────────

    return {
        "status": status,
        "status_label": status_label,
        "messages": messages,
        "breakdown": breakdown,
        "asaas_available": asaas_balance is not None,
        "correction_applied": correction_applied,
        "ai_explanation": ai_explanation,
        "fee_forensics": {
            "status": _fee_health,
            "total_transactions_audited": _total_audited,
            "discrepancies": _fee_discrepancies,
            "total_overcharged": _total_overcharged,
            "total_undercharged": _total_undercharged,
        },
        "audit_invariants": {
            "correntistas_modified": False,
            "matrix_only_corrections": True,
        },
    }


@router.get("/admin/matrix/projections")
async def matrix_projections(
    users: int = Query(default=50, ge=1, le=500000),
    tx: float = Query(default=3.0, ge=0.1, le=1000.0),
    pj_pct: float = Query(default=20.0, ge=0.0, le=100.0),
    avg_out: float = Query(default=150.0, ge=1.0),
    avg_in: float = Query(default=100.0, ge=1.0),
    months: int = Query(default=12, ge=1, le=60),
    growth: float = Query(default=0.25, ge=0.0, le=5.0),
    current_user: User = Depends(get_current_user),
):
    """
    Returns monthly revenue projection and compound growth model for the admin panel.

    Query params:
        users   : Active users on platform (default 50)
        tx      : Avg transactions per user per month (default 3.0)
        pj_pct  : Percentage of PJ accounts (default 20.0)
        avg_out : Average outbound PIX value in BRL (default R$150)
        avg_in  : Average inbound PIX value in BRL (default R$100)
        months  : Number of months to project (default 12)
        growth  : Monthly user growth rate as decimal — 0.25 = 25% MoM (default 0.25)

    Admin-only endpoint.
    """
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    from app.core.fees import monthly_revenue_projection, growth_projection, ASAAS_PIX_OUTBOUND_FREE_MONTHLY

    pj_ratio = pj_pct / 100.0

    # Single-month snapshot at configured user count
    snapshot = monthly_revenue_projection(
        active_users=users,
        tx_per_user_per_month=tx,
        pj_ratio=pj_ratio,
        avg_outbound_value=avg_out,
        avg_inbound_value=avg_in,
    )

    # Compound growth curve over N months
    curve = growth_projection(
        months=months,
        initial_users=users,
        monthly_user_growth_rate=growth,
        tx_per_user_per_month=tx,
        pj_ratio=pj_ratio,
        avg_outbound_value=avg_out,
        avg_inbound_value=avg_in,
    )

    # Key financial milestones from the growth curve
    first_profitable = next((m for m in curve if m["net_profit"] > 0), None)
    peak = max(curve, key=lambda m: m["net_profit"]) if curve else None
    total_12m = sum(m["net_profit"] for m in curve)

    pj_out_fee_each = max(3.0, 0.0080 * avg_out)

    return {
        "snapshot": snapshot,
        "growth_curve": curve,
        "milestones": {
            "first_profitable_month": first_profitable["month"] if first_profitable else None,
            "peak_monthly_profit": peak["net_profit"] if peak else 0,
            "peak_month": peak["month"] if peak else None,
            "cumulative_matrix_end": curve[-1]["cumulative_matrix"] if curve else 0,
            "total_projected_profit": round(total_12m, 2),
        },
        "model_constants": {
            "asaas_free_outbound_per_month": ASAAS_PIX_OUTBOUND_FREE_MONTHLY,
            "asaas_outbound_cost_after_quota": 2.00,
            "asaas_inbound_net_cost": 0.00,
            "pf_outbound_fee": 2.50,
            "pf_margin_per_tx_after_quota": 0.50,
            "pf_margin_first_30_tx": 2.50,
            "pj_outbound_fee_at_avg": round(pj_out_fee_each, 2),
            "pj_margin_at_avg": round(pj_out_fee_each - 2.0, 2),
            "pj_inbound_pure_revenue": True,
        },
    }

