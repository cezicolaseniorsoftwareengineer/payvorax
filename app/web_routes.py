from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.core.config import settings
from pydantic import BaseModel
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
    return templates.TemplateResponse(
        "pix.html",
        {"request": request, "page": "pix", "user_name": current_user.name}
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

    users = db.query(User).order_by(User.created_at.desc()).all()

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "page": "admin",
        "user_name": current_user.name,
        "users": users,
        "total": len(users),
        "active": sum(1 for u in users if u.is_active),
        "verified_docs": sum(1 for u in users if u.document_verified),
        "verified_emails": sum(1 for u in users if u.email_verified),
    })


class ToggleActiveRequest(BaseModel):
    active: bool


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
