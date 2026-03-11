from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from decimal import Decimal
from uuid import uuid4
from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.core.config import settings
from app.core.fees import is_pj as _is_pj, calculate_pix_fee, calculate_boleto_fee, fee_display
from app.pix.internal_transfer import find_recipient_user, execute_internal_transfer
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
    user_pj = _is_pj(current_user.cpf_cnpj)
    pix_fee_pf_label = fee_display(calculate_pix_fee(current_user.cpf_cnpj, 100, is_external=True))
    return templates.TemplateResponse(
        "pix.html",
        {
            "request": request,
            "page": "pix",
            "user_name": current_user.name,
            "user_is_pj": user_pj,
            "user_balance": float(current_user.balance),
            "pix_fee_external_fixed": "R$ 0,25" if not user_pj else None,
            "pix_fee_external_rate": "0,5% (min R$ 0,50)" if user_pj else None,
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
    })


class ToggleActiveRequest(BaseModel):
    active: bool


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
            raise ValueError(f"Tipo de chave invalido. Aceitos: {', '.join(sorted(valid))}")
        return v.upper()


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
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")
    if target.email == settings.ADMIN_EMAIL:
        raise HTTPException(status_code=400, detail="Conta admin nao pode ser suspensa.")

    target.is_active = payload.active
    db.commit()
    return {"ok": True, "user_id": user_id, "is_active": payload.active}


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
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")
    if target.email == settings.ADMIN_EMAIL:
        raise HTTPException(status_code=400, detail="Conta admin nao pode ser excluida.")
    if target.email == settings.MATRIX_ACCOUNT_EMAIL:
        raise HTTPException(status_code=400, detail="Conta matriz nao pode ser excluida.")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Nao e possivel excluir a propria conta.")

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


@router.post("/admin/matrix/transfer")
async def matrix_transfer(
    payload: MatrixTransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Transfer accumulated fee balance from the matrix account to a PIX key.
    Detects internal Bio Code Tech Pay recipients and credits them directly without gateway.
    Admin only.
    """
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    matrix = db.query(User).filter(User.email == settings.MATRIX_ACCOUNT_EMAIL).first()
    if not matrix:
        raise HTTPException(status_code=404, detail="Conta matriz nao encontrada.")

    if matrix.balance < payload.amount:
        raise HTTPException(
            status_code=400,
            detail=f"Saldo insuficiente. Disponivel: R$ {matrix.balance:.2f}"
        )

    idempotency_key = str(uuid4())
    correlation_id = str(uuid4())
    payment_id = None

    # Resolve key_type to enum for internal lookup
    try:
        key_type_enum = PixKeyType(payload.key_type.upper())
    except ValueError:
        key_type_enum = None

    # Check if destination is an internal Bio Code Tech Pay account
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
                description=payload.description or "Repasse Bio Code Technology",
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
            description=payload.description or "Repasse Bio Code Technology",
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
    Balance audit: compares sum of all user balances against Asaas account balance.
    Returns structured breakdown for the Matrix Dashboard audit panel.
    Admin only.
    """
    if current_user.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Acesso restrito.")

    all_users = db.query(User).all()
    matrix_user = next((u for u in all_users if u.email == settings.MATRIX_ACCOUNT_EMAIL), None)
    customers = [u for u in all_users if u.email != settings.MATRIX_ACCOUNT_EMAIL]

    internal_sum = sum(float(u.balance) for u in customers)
    matrix_balance = float(matrix_user.balance) if matrix_user else 0.0
    total_internal = internal_sum + matrix_balance

    # Try to fetch Asaas account balance
    asaas_balance: float | None = None
    from app.adapters.gateway_factory import get_payment_gateway
    gateway = get_payment_gateway()
    if gateway and hasattr(gateway, "_make_request"):
        try:
            resp = gateway._make_request("GET", "/finance/balance")
            asaas_balance = float(resp.get("balance", 0))
        except Exception:
            asaas_balance = None

    breakdown = [
        {"label": "Saldo clientes (soma)", "value": round(internal_sum, 2), "highlight": False},
        {"label": "Saldo Conta Matrix", "value": round(matrix_balance, 2), "highlight": True},
        {"label": "Total interno (clientes + matrix)", "value": round(total_internal, 2), "highlight": False},
    ]

    messages = []
    status = "OK"
    status_label = "Saldos consistentes"

    if asaas_balance is not None:
        breakdown.append({"label": "Saldo Asaas (conta real)", "value": round(asaas_balance, 2), "highlight": False})
        diff = abs(total_internal - asaas_balance)
        breakdown.append({"label": "Diferenca (interno vs Asaas)", "value": round(diff, 2), "highlight": diff > 0})
        if diff < 0.01:
            messages.append("Saldo interno e Asaas estao sincronizados.")
        elif diff < 10:
            status = "WARN"
            status_label = "Divergencia pequena detectada"
            messages.append(f"Diferenca de R$ {diff:.2f} entre saldo interno e Asaas. Verifique transacoes pendentes.")
        else:
            status = "ERROR"
            status_label = "Divergencia critica de saldo"
            messages.append(f"Divergencia de R$ {diff:.2f}. Reconciliacao urgente necessaria.")
    else:
        messages.append("Asaas indisponivel ou nao configurado. Auditoria parcial (apenas saldos internos).")
        status = "WARN"
        status_label = "Auditoria parcial — Asaas nao acessivel"

    messages.append(f"Total de {len(customers)} correntistas ativos no sistema.")

    return {
        "status": status,
        "status_label": status_label,
        "messages": messages,
        "breakdown": breakdown,
        "asaas_available": asaas_balance is not None,
    }
