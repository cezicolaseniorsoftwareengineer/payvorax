from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.pix.service import listar_extrato
from app.auth.dependencies import get_current_user
from app.auth.models import User
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


@router.get("/", response_class=HTMLResponse)
async def read_root(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Main Dashboard (Home)"""
    extrato = listar_extrato(db, current_user.id)
    saldo = extrato["saldo"]

    return templates.TemplateResponse("index.html", {
        "request": request,
        "page": "home",
        "saldo": saldo,
        "user_name": current_user.nome
    })


@router.get("/ui/pix", response_class=HTMLResponse)
async def pix_ui(request: Request, current_user: User = Depends(get_current_user)):
    """PIX Interface"""
    return templates.TemplateResponse(
        "pix.html",
        {"request": request, "page": "pix", "user_name": current_user.nome}
    )


@router.get("/ui/parcelamento", response_class=HTMLResponse)
async def parcelamento_ui(request: Request, current_user: User = Depends(get_current_user)):
    """Simulation Interface"""
    return templates.TemplateResponse(
        "parcelamento.html",
        {"request": request, "page": "parcelamento", "user_name": current_user.nome}
    )


@router.get("/pix/pagar-qrcode", response_class=HTMLResponse)
async def pix_payment_simulation(request: Request, current_user: User = Depends(get_current_user)):
    """QR Code Payment Simulation Page"""
    return templates.TemplateResponse(
        "pix_payment.html",
        {"request": request, "page": "pix_payment", "user_name": current_user.nome}
    )


@router.get("/ui/extrato", response_class=HTMLResponse)
async def extrato_ui(request: Request, current_user: User = Depends(get_current_user)):
    """Statement Interface"""
    return templates.TemplateResponse("extrato.html", {"request": request, "page": "extrato", "user_name": current_user.nome})
