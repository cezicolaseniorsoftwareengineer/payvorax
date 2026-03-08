"""
Centralized application configuration implementing the 12-Factor App methodology.
Enforces strict environment separation and security protocols.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


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

    # Asaas Payment Gateway Configuration
    # CRITICAL: NEVER commit this to Git. Set in environment or .env file.
    # Format: $aact_prod_... (production) or $aact_sandbox_... (sandbox)
    ASAAS_API_KEY: Optional[str] = None
    ASAAS_USE_SANDBOX: bool = True  # Set to False in production
    # Set to true ONLY to run real-money integration tests against Asaas.
    # These tests require valid CPF/CNPJ and real Asaas credentials.
    ASAAS_INTEGRATION_TESTS: bool = False
    # Webhook authentication token — must match the token configured in Asaas Dashboard.
    # Generate via: Asaas > Configuracoes > Integracoes > Webhooks > Token de autenticacao
    # Then set this value in Render Dashboard as ASAAS_WEBHOOK_TOKEN
    ASAAS_WEBHOOK_TOKEN: Optional[str] = None

    # Admin access — only this email gets the admin panel
    ADMIN_EMAIL: str = "biocodetechnology@gmail.com"

    # Transactional email via Resend (https://resend.com)
    # CRITICAL: set only via environment variable — never hardcode this value.
    RESEND_API_KEY: Optional[str] = None
    RESEND_FROM_NAME: str = "PayvoraX"
    RESEND_FROM_EMAIL: str = "noreply@payvorax.com"
    # Base URL used to build verification links (e.g. https://payvorax.onrender.com)
    APP_BASE_URL: str = "http://localhost:8000"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


settings = Settings()
