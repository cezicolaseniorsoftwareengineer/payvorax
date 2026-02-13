"""
Fintech Tech Challenge - Enterprise FastAPI Application.
Implements Domain-Driven Design (DDD) and Hexagonal Architecture.
Features strict input validation, audit logging, and distributed tracing.
"""
import os
import sys
from pathlib import Path
from typing import Callable, Awaitable, Dict, Any

# STRICT STARTUP ENFORCEMENT
# The application must be started via `python start.py`
if not os.environ.get("PAYVORAX_ALLOWED_START") and "pytest" not in sys.modules:
    # Allow production environments (Render/PythonAnywhere) to bypass if needed,
    # but for local dev, enforce start.py.
    # Checking for common production env vars or if explicitly disabled.
    if not os.environ.get("RENDER") and not os.environ.get("PYTHONANYWHERE_DOMAIN"):
        print("\n\033[91mCRITICAL ERROR: Forbidden Startup Method.\033[0m")
        print("You must use the standardized entry point:")
        print("   > python start.py")
        print("\nDirect execution via uvicorn or other methods is prohibited to ensure environment consistency.\n")
        sys.exit(1)

from fastapi import FastAPI, Request, Response, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
from app.cards.router import router as cards_router
from app.pix.router import router as pix_router
from app.antifraude.router import router as antifraude_router
from app.web_routes import router as web_router
from app.auth.router import router as auth_router
from app.boleto.router import router as boleto_router
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management (startup/shutdown hooks)."""
    # Startup
    logger.info(f"Initializing {settings.APP_NAME} v{settings.VERSION}")
    init_db()
    logger.info("Database initialized")

    yield

    # Shutdown
    logger.info("Shutting down application")


# FastAPI Application Factory
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description=(
        "Enterprise-grade financial system implementing DDD and Hexagonal Architecture. "
        "See README.md for full documentation."
    ),
    lifespan=lifespan
)

# Trust X-Forwarded-Proto headers from Render's Load Balancer
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Middleware for distributed tracing and logging
@app.middleware("http")
async def add_security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """
    Security Middleware to add HTTP security headers to every response.
    Protects against XSS, Clickjacking, and MIME-sniffing.
    """
    response = await call_next(request)

    # HSTS (HTTP Strict Transport Security) - Force HTTPS for 1 year
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    # Prevent Clickjacking (X-Frame-Options)
    response.headers["X-Frame-Options"] = "DENY"

    # XSS Protection
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"

    # Referrer Policy
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    return response


@app.middleware("http")
async def add_correlation_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """
    Middleware for distributed tracing.
    Injects a unique Correlation ID into the request context and propagates it to the response headers.
    """
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

    logger.info(
        f"Response: {response.status_code} | {process_time:.3f}s",
        extra={"correlation_id": correlation_id}
    )

    return response


# Router Registration
app.include_router(cards_router, prefix="/cards", tags=["Cards"])
app.include_router(pix_router, prefix="/pix", tags=["PIX"])
app.include_router(antifraude_router, prefix="/antifraud", tags=["Anti-Fraud"])
app.include_router(auth_router, prefix="/auth", tags=["Auth"])
app.include_router(boleto_router, tags=["Boleto"])

# Mount Static Files
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Web UI Router (Frontend)
app.include_router(web_router, tags=["Web UI"])


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Serve favicon to avoid noisy 404s from browsers."""
    candidate = Path(__file__).resolve().parent / "static" / "img" / "logo.png"
    if candidate.exists():
        return FileResponse(path=str(candidate), media_type="image/png")
    return Response(status_code=204)


# API Info Endpoint (Moved from root)
@app.get("/api-info", tags=["Health"])
def api_info() -> Dict[str, Any]:
    """
    Endpoint exposing API metadata and service discovery links.
    """
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
            "redoc": "/redoc"
        }
    }


@app.get("/health", tags=["Health"])
def health_check(db: Session = Depends(get_db)) -> Dict[str, str]:
    """
    Readiness probe and database warm-up endpoint.
    Executes a lightweight query to wake the Neon serverless compute
    and keep the connection pool warm. Called silently by the frontend
    on every page load to eliminate cold-start latency for user actions.
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
        "version": settings.VERSION
    }


# Global Exception Handler
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    Handle HTTP exceptions, including 401 Unauthorized for browser redirects.
    """
    correlation_id = getattr(request.state, "correlation_id", "N/A")
    accept = request.headers.get("accept", "")

    logger.info(
        f"HTTPException: {exc.status_code} | Accept: {accept}",
        extra={"correlation_id": correlation_id}
    )

    # Handle 401 Unauthorized for Browser Requests (Redirect to Login)
    if exc.status_code == 401 and "text/html" in accept:
        logger.info("Redirecting to /login", extra={"correlation_id": correlation_id})
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie("access_token")
        return response

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "correlation_id": correlation_id}
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Global exception barrier.
    Captures unhandled exceptions, logs stack traces with Correlation IDs,
    and returns a sanitized 500 Internal Server Error response.
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
            "correlation_id": correlation_id
        }
    )
