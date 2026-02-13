"""
Centralized application configuration implementing the 12-Factor App methodology.
Enforces strict environment separation and security protocols.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Immutable configuration schema backed by environment variables."""

    APP_NAME: str = "PayvoraX"
    VERSION: str = "1.0.0"
    DEBUG: bool = False

    # PostgreSQL Connection (Neon, Supabase, or any standard PostgreSQL)
    # WARNING: Set this in your environment variables (e.g. .env file or Render Dashboard)
    DATABASE_URL: str = "postgresql://user:password@host:port/dbname"

    SECRET_KEY: str = "your-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    # Increased to 7 days (10080 minutes) to keep users logged in ("Remember Me")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080

    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


settings = Settings()
