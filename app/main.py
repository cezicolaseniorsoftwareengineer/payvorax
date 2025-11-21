"""
Fintech Tech Challenge - Enterprise FastAPI Application.
Implements Domain-Driven Design (DDD) and Hexagonal Architecture.
Features strict input validation, audit logging, and distributed tracing.
"""
from typing import Callable, Awaitable, Dict, Any
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from contextlib import asynccontextmanager
import time
from uuid import uuid4

from app.core.config import settings
from app.core.database import init_db
from app.core.logger import logger
from app.parcelamento.router import router as parcelamento_router
from app.pix.router import router as pix_router
from app.antifraude.router import router as antifraude_router
from app.web_routes import router as web_router
from app.auth.router import router as auth_router
from app.boleto.router import router as boleto_router
from fastapi.staticfiles import StaticFiles
import os
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
# This fixes the "http vs https" redirect mismatch for Google OAuth
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Session Middleware (Required for OAuth2)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

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
app.include_router(parcelamento_router, prefix="/parcelamento", tags=["Installment"])
app.include_router(pix_router, prefix="/pix", tags=["PIX"])
app.include_router(antifraude_router, prefix="/antifraude", tags=["Anti-Fraud"])
app.include_router(auth_router, prefix="/auth", tags=["Auth"])
app.include_router(boleto_router, tags=["Boleto"])

# Mount Static Files
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Web UI Router (Frontend)
app.include_router(web_router, tags=["Web UI"])


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
            "parcelamento": "/parcelamento/simular",
            "pix_create": "/pix/create",
            "pix_confirm": "/pix/confirm",
            "pix_statement": "/pix/statement",
            "antifraude": "/antifraude/analisar",
            "docs": "/docs",
            "redoc": "/redoc"
        }
    }


@app.get("/health", tags=["Health"])
def health_check() -> Dict[str, str]:
    """
    Liveness probe endpoint for orchestration systems.
    """
    return {
        "status": "healthy",
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
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # nosec
        port=8000,
        reload=settings.DEBUG
    )
