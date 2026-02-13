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

    Supports multiple PostgreSQL providers:
    - Neon (*.neon.tech) — serverless, auto-suspend after inactivity
    - Supabase pooler (*.pooler.supabase.com) — multi-tenant routing
    - Any standard PostgreSQL instance
    """
    url = make_url(database_url)
    host = (url.host or "").lower()
    username = url.username or ""

    # SQLite specifics
    if url.drivername.startswith("sqlite"):
        return {
            "connect_args": {"check_same_thread": False},
        }

    # --- Provider-specific validation ---

    # Supabase pooler: requires tenant suffix in username
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
                "includes the project ref suffix (example: 'postgres.<project_ref>'). "
                "Also verify the Supabase project is not paused (free-tier projects pause after 7 days of inactivity)."
            )
        else:
            logger.info(f"Supabase pooler detected (host={host}).")

    # Neon: log detection for operational awareness
    if host.endswith(".neon.tech"):
        logger.info(f"Neon serverless PostgreSQL detected (host={host}).")

    # --- SSL enforcement for cloud providers ---
    connect_args: Dict[str, Any] = {}
    cloud_hosts = (".supabase.co", ".supabase.com", ".neon.tech")
    if any(host.endswith(suffix) for suffix in cloud_hosts):
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


def init_db(max_retries: int = 5, base_delay: float = 2.0) -> None:
    """
    Idempotent initialization of database schema artifacts.

    Implements exponential backoff to survive transient connection failures
    (e.g. Supabase pooler cold-start, DNS resolution delays, network blips).
    Raises the last exception after exhausting all retries.
    """
    import time

    last_exception: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "Iniciando criação de tabelas no banco de dados",
                extra={"attempt": attempt, "max_retries": max_retries},
            )
            Base.metadata.create_all(bind=engine)
            logger.info("Tabelas criadas com sucesso")
            return
        except Exception as exc:
            last_exception = exc
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                f"Database connection failed (attempt {attempt}/{max_retries}). "
                f"Retrying in {delay:.1f}s. Error: {exc}",
            )
            if attempt < max_retries:
                time.sleep(delay)

    # All retries exhausted — let the application crash with a clear message.
    logger.critical(
        f"Database initialization failed after {max_retries} attempts. "
        "Verify DATABASE_URL, database provider status (Neon/Supabase), and network connectivity."
    )
    raise RuntimeError(
        f"Could not initialize database after {max_retries} attempts."
    ) from last_exception
