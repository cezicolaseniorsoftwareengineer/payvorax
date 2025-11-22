from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.pix.service import list_statement
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
    statement = list_statement(db, current_user.id)
    balance = statement["balance"]

    return templates.TemplateResponse("index.html", {
        "request": request,
        "page": "home",
        "balance": balance,
        "user_name": current_user.name
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
