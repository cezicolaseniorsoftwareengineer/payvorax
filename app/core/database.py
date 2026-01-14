"""
Database connection management and ORM session factory.
Supports dialect abstraction for SQLite and PostgreSQL.
"""
from typing import Any, Dict

from sqlalchemy import create_engine, event
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.core.logger import logger


def _redact_sqlalchemy_url(url: URL) -> str:
    """Return a safe-to-log connection string (never prints passwords)."""
    try:
        return url.render_as_string(hide_password=True)
    except Exception:
        # Fallback: do not risk leaking secrets.
        return "<unavailable>"


def _build_engine_kwargs(database_url: str) -> Dict[str, Any]:
    """
    Build SQLAlchemy engine kwargs with guardrails for common production issues.

    Key focus:
    - Supabase pooler requires tenant routing in the username (e.g. 'postgres.<project_ref>').
    - Supabase requires SSL in most setups.
    - pool_pre_ping improves resilience against stale connections.
    """
    url = make_url(database_url)
    host = (url.host or "").lower()
    username = url.username or ""

    # SQLite specifics
    if url.drivername.startswith("sqlite"):
        return {
            "connect_args": {"check_same_thread": False},
        }

    # Supabase pooler specifics (multi-tenant router)
    # Observed failure mode: FATAL: Tenant or user not found
    # Typically caused by missing '<tenant>' suffix in username when using pooler host.
    if host.endswith(".pooler.supabase.com"):
        if "." not in username:
            safe_url = _redact_sqlalchemy_url(url)
            logger.critical(
                "Invalid Supabase pooler DATABASE_URL: username missing tenant suffix. "
                "Expected '<db_user>.<project_ref>' when using '*.pooler.supabase.com'. "
                f"Current (redacted): {safe_url}"
            )
            raise RuntimeError(
                "Invalid DATABASE_URL for Supabase pooler. "
                "Use the Supabase 'Transaction pooler' connection string and ensure the username "
                "includes the project ref suffix (example: 'postgres.<project_ref>')."
            )

    connect_args: Dict[str, Any] = {}
    # Enforce SSL for Supabase endpoints if not explicitly set in the URL query.
    if host.endswith(".supabase.co") or host.endswith(".supabase.com"):
        if "sslmode" not in (url.query or {}):
            connect_args["sslmode"] = "require"

    engine_kwargs: Dict[str, Any] = {
        "pool_pre_ping": True,
    }
    if connect_args:
        engine_kwargs["connect_args"] = connect_args

    return engine_kwargs

engine = create_engine(settings.DATABASE_URL, **_build_engine_kwargs(settings.DATABASE_URL))

# Enable Write-Ahead Logging (WAL) for SQLite to handle concurrency better
if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Yields a thread-safe database session context. Ensures connection closure upon completion."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Idempotent initialization of database schema artifacts."""
    logger.info("Iniciando criação de tabelas no banco de dados")
    Base.metadata.create_all(bind=engine)
    logger.info("Tabelas criadas com sucesso")
