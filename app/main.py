"""
Fintech Tech Challenge - Enterprise FastAPI Application.
Implements Domain-Driven Design (DDD) and Hexagonal Architecture.
Features strict input validation, audit logging, and distributed tracing.
"""
import os
import sys
import asyncio
from pathlib import Path
from typing import Callable, Awaitable, Dict, Any

# STRICT STARTUP ENFORCEMENT
# The application must be started via `python start.py`
if not os.environ.get("BIO_CODE_TECH_PAY_ALLOWED_START") and "pytest" not in sys.modules:
    if not os.environ.get("RENDER") and not os.environ.get("PYTHONANYWHERE_DOMAIN"):
        print("\n\033[91mCRITICAL ERROR: Forbidden Startup Method.\033[0m")
        print("You must use the standardized entry point:")
        print("   > python start.py")
        print("\nDirect execution via uvicorn or other methods is prohibited to ensure environment consistency.\n")
        sys.exit(1)

from fastapi import FastAPI, Request, Response, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from contextlib import asynccontextmanager
import time
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import init_db, get_db
from app.core.logger import logger
from app.core.matrix import seed_matrix_account
from app.core.audit_worker import balance_audit_loop
from app.cards.router import router as cards_router
from app.pix.router import router as pix_router
from app.antifraude.router import router as antifraude_router
from app.web_routes import router as web_router
from app.auth.router import router as auth_router
from app.boleto.router import router as boleto_router
from app.minha_conta.router import router as minha_conta_router
import app.minha_conta.models  # noqa: F401 — registers UserSubscription in Base.metadata
import app.ia.ai_interactions  # noqa: F401 — registers AiInteraction in Base.metadata
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter, Histogram

REQUEST_COUNT = Counter('http_requests_total', 'Total HTTP requests', ['method', 'endpoint', 'status'])
REQUEST_LATENCY = Histogram('http_request_duration_seconds', 'HTTP request latency', ['method', 'endpoint'])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management (startup/shutdown hooks)."""
    logger.info(f"Initializing {settings.APP_NAME} v{settings.VERSION}")
    init_db()
    logger.info("Database initialized")
    seed_matrix_account()
    logger.info("Matrix account ready")

    trace.set_tracer_provider(TracerProvider())
    trace.get_tracer_provider().add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())
    )
    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument()

    from app.core.database import SessionLocal
    from app.adapters.gateway_factory import get_payment_gateway
    _audit_task = asyncio.create_task(
        balance_audit_loop(SessionLocal, get_payment_gateway)
    )
    logger.info("Balance audit worker started")

    yield

    _audit_task.cancel()
    logger.info("Shutting down application")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description=(
        "Enterprise-grade financial system implementing DDD and Hexagonal Architecture. "
        "See README.md for full documentation."
    ),
    lifespan=lifespan
)

_TRUSTED_PROXIES = ["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"] if not os.environ.get("RENDER") else "*"
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_TRUSTED_PROXIES)

_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
] or [
    "https://new-credit-fintech.onrender.com",
    "https://biocodetechpay.onrender.com",
]
if settings.DEBUG:
    _ALLOWED_ORIGINS += ["http://localhost:8000", "http://127.0.0.1:8000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Adds HTTP security headers to every response."""
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com https://html2canvas.hertzen.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://openrouter.ai; "
        "frame-ancestors 'none'"
    )
    return response


@app.middleware("http")
async def add_correlation_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Injects a unique Correlation ID into every request and propagates it to the response."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
    request.state.correlation_id = correlation_id

    start_time = time.time()
    logger.info(
        f"Request: {request.method} {request.url.path}",
        extra={"correlation_id": correlation_id}
    )

    response = await call_next(request)

    process_time = time.time() - start_time
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Process-Time"] = str(process_time)

    REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path, status=response.status_code).inc()
    REQUEST_LATENCY.labels(method=request.method, endpoint=request.url.path).observe(process_time)

    logger.info(
        f"Response: {response.status_code} | {process_time:.3f}s",
        extra={"correlation_id": correlation_id}
    )
    return response


@app.get("/health", tags=["Health"])
def health_check(db: Session = Depends(get_db)) -> Dict[str, str]:
    """
    Readiness probe and database warm-up endpoint.
    Executes a lightweight query to wake the Neon serverless compute
    and keep the connection pool warm.
    """
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "degraded"

    return {
        "status": "healthy",
        "db": db_status,
        "app": settings.APP_NAME,
        "version": settings.VERSION,
    }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(cards_router, prefix="/cards", tags=["Cards"])
app.include_router(pix_router, prefix="/pix", tags=["PIX"])
app.include_router(antifraude_router, prefix="/antifraud", tags=["Anti-Fraud"])
app.include_router(auth_router, prefix="/auth", tags=["Auth"])
app.include_router(boleto_router, tags=["Boleto"])

from app.ia.router import router as ia_router
app.include_router(ia_router)
app.include_router(minha_conta_router, tags=["Minha Conta"])

from app.core.metrics import router as metrics_router
app.include_router(metrics_router, tags=["Metrics"])

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(web_router, tags=["Web UI"])


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Serve favicon to avoid noisy 404s from browsers."""
    candidate = Path(__file__).resolve().parent / "static" / "img" / "logo.png"
    if candidate.exists():
        return FileResponse(path=str(candidate), media_type="image/png")
    return Response(status_code=204)


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    """
    Serve PWA Service Worker from root scope.
    Must be at / for full-scope registration — cannot be served from /static/.
    """
    candidate = Path(__file__).resolve().parent / "static" / "sw.js"
    if candidate.exists():
        return FileResponse(
            path=str(candidate),
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"},
        )
    return Response(status_code=204)


@app.get("/api-info", tags=["Health"])
def api_info() -> Dict[str, Any]:
    """Endpoint exposing API metadata and service discovery links."""
    return {
        "app": settings.APP_NAME,
        "version": settings.VERSION,
        "status": "online",
        "endpoints": {
            "ui": "/",
            "cards": "/cards",
            "pix_create": "/pix/create",
            "pix_confirm": "/pix/confirm",
            "pix_statement": "/pix/statement",
            "antifraud": "/antifraud/analyze",
            "docs": "/docs",
            "redoc": "/redoc",
        },
    }


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handles HTTP exceptions including 401 browser redirects."""
    correlation_id = getattr(request.state, "correlation_id", "N/A")
    accept = request.headers.get("accept", "")

    logger.info(
        f"HTTPException: {exc.status_code} | Accept: {accept}",
        extra={"correlation_id": correlation_id}
    )

    if exc.status_code == 401 and "text/html" in accept:
        logger.info("Redirecting to /login", extra={"correlation_id": correlation_id})
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie("access_token")
        return response

    if exc.status_code == 404 and "text/html" in accept:
        logger.info("404 browser request — redirecting to /login", extra={"correlation_id": correlation_id})
        return RedirectResponse(url="/login", status_code=302)

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "correlation_id": correlation_id}
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Global exception barrier.
    Captures unhandled exceptions, logs stack traces with Correlation IDs,
    and returns a sanitized 500 response.
    """
    correlation_id = getattr(request.state, "correlation_id", "N/A")
    logger.error(
        f"Unhandled exception: {str(exc)}",
        exc_info=True,
        extra={"correlation_id": correlation_id}
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error",
            "correlation_id": correlation_id,
        },
    )
