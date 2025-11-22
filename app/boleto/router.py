from fastapi import APIRouter, Depends, HTTPException, Request, Header
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from uuid import uuid4

from app.core.database import get_db
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.boleto.schemas import BoletoQuery, BoletoDetails, BoletoPaymentRequest, PaymentResponse
from app.boleto.service import query_boleto, process_payment
from app.pix.service import get_balance

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/ui/boleto", response_class=HTMLResponse)
async def view_boleto(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    balance = get_balance(db, current_user.id)
    return templates.TemplateResponse("boleto.html", {
        "request": request,
        "user_name": current_user.name,
        "balance": balance,
        "page": "boleto"
    })


@router.post("/api/boleto/query", response_model=BoletoDetails)
def api_query_boleto(
    data: BoletoQuery,
    current_user: User = Depends(get_current_user)
):
    try:
        return query_boleto(data.barcode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/boleto/pay", response_model=PaymentResponse)
def api_pay_boleto(
    data: BoletoPaymentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_correlation_id: str = Header(default=None)
):
    correlation_id = x_correlation_id or str(uuid4())
    try:
        boleto = process_payment(db, data, current_user.id, correlation_id)
        return PaymentResponse(
            id=boleto.id,
            status="SUCCESS",
            message="Payment successful",
            receipt=f"COMP-{boleto.id[:8].upper()}"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error processing payment")
