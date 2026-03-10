"""
Centralized application configuration implementing the 12-Factor App methodology.
Enforces strict environment separation and security protocols.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    """Immutable configuration schema backed by environment variables."""

    APP_NAME: str = "Bio Code Tech Pay"
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
    # Operation key for Asaas API transfers (bypasses 2FA/manual authorization).
    # Configure in Asaas Dashboard → Configuracoes → Seguranca → Chave de Operacao.
    # Then set this value in Render Dashboard as ASAAS_OPERATION_KEY.
    # NOTE: if ASAAS_TOTP_SECRET is set, this static key is ignored in favour of TOTP codes.
    ASAAS_OPERATION_KEY: Optional[str] = None
    # TOTP secret for device-based authorization in Asaas.
    # When Asaas has “Autorizacao por dispositivo” enabled, every transfer requires
    # the current 6-digit TOTP code (rotates every 30s) as the operationKey.
    # To obtain the secret:
    #   1. Go to Asaas → Configuracoes → Seguranca → Autorizacao por dispositivo.
    #   2. Click “Configurar dispositivo” or “Ver segredo”.
    #   3. Copy the base32 secret (the text behind the QR code).
    #   4. Set ASAAS_TOTP_SECRET in Render Dashboard with that value.
    # The backend will call pyotp.TOTP(secret).now() at transfer time automatically.
    ASAAS_TOTP_SECRET: Optional[str] = None
    # Token for Asaas withdrawal validation webhook (Mecanismos de seguranca → Validacao de saque).
    # Optional — leave empty to accept all requests, or set to validate the incoming token.
    # Configure in Render Dashboard as ASAAS_WITHDRAWAL_VALIDATION_TOKEN.
    # URL to register in Asaas: <APP_BASE_URL>/pix/webhook/asaas/validacao-saque
    ASAAS_WITHDRAWAL_VALIDATION_TOKEN: Optional[str] = None

    # Admin access — only this email gets the admin panel
    ADMIN_EMAIL: str = "biocodetechnology@gmail.com"

    # Matrix (fee-collection) account — internal system account, never exposed to end users
    MATRIX_ACCOUNT_EMAIL: str = "matrix@biocodetechpay.internal"
    MATRIX_ACCOUNT_CNPJ: str = "00000000000100"   # Internal identity — not a real CNPJ
    MATRIX_ACCOUNT_NAME: str = "Bio Code Technology"

    # OpenRouter API key — required for the BIO TECH PAY I.A chat agent.
    # Set in Render Dashboard as OPENROUTER_API_KEY. Never commit this value.
    OPENROUTER_API_KEY: Optional[str] = None

    # Transactional email via Resend (https://resend.com)
    # CRITICAL: set only via environment variable — never hardcode this value.
    RESEND_API_KEY: Optional[str] = None
    RESEND_FROM_NAME: str = "Bio Code Tech Pay"
    RESEND_FROM_EMAIL: str = "onboarding@resend.dev"
    # Base URL used to build verification links (e.g. https://Bio Code Tech Pay.onrender.com)
    APP_BASE_URL: str = "http://localhost:8000"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


settings = Settings()
