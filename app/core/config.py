"""
Centralized application configuration implementing the 12-Factor App methodology.
Enforces strict environment separation and security protocols.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator
from typing import Optional


class Settings(BaseSettings):
    """Immutable configuration schema backed by environment variables."""

    APP_NAME: str = "BioCodeTechPay"
    VERSION: str = "1.0.0"
    DEBUG: bool = False

    # PostgreSQL Connection (Neon, Supabase, or any standard PostgreSQL)
    # WARNING: Set this in your environment variables (e.g. .env file or Render Dashboard)
    DATABASE_URL: str = "postgresql://user:password@host:port/dbname"

    SECRET_KEY: str = "your-secret-key-change-in-production"

    @model_validator(mode="after")
    def _enforce_secret_key(self):
        """Fail-fast: refuse to start with the known insecure default SECRET_KEY."""
        import sys
        _INSECURE_DEFAULTS = {"your-secret-key-change-in-production", "changeme", "secret", ""}
        if "pytest" not in sys.modules and self.SECRET_KEY in _INSECURE_DEFAULTS:
            raise ValueError(
                "FATAL: SECRET_KEY is set to an insecure default. "
                "Set a strong, unique SECRET_KEY via environment variable before starting the application."
            )
        return self
    ALGORITHM: str = "HS256"
    # Access token: short-lived (15 min). Session continuity handled by refresh token.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    # Refresh token: long-lived (7 days). Rotated on every use. httpOnly cookie.
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 10080

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
    # BioCodeTechPay platform Asaas Wallet ID — used for automatic fee split on PIX charges.
    # Find in: Asaas Dashboard → Configuracoes → Wallet ID.
    # When set, every PIX cobranca automatically splits the inbound platform fee
    # (R$3.00: R$2 rede + R$1 manutencao) to this wallet via Asaas split API.
    # This guarantees fee collection at infrastructure level, independent of webhook logic.
    # Set in Render Dashboard as ASAAS_PLATFORM_WALLET_ID.
    ASAAS_PLATFORM_WALLET_ID: Optional[str] = None
    ADMIN_EMAIL: str = "biocodetechnology@gmail.com"

    # Matrix (fee-collection) account — internal system account, never exposed to end users
    MATRIX_ACCOUNT_EMAIL: str = "matrix@biocodetechpay.internal"
    MATRIX_ACCOUNT_CNPJ: str = "00000000000100"   # Internal identity — not a real CNPJ
    MATRIX_ACCOUNT_NAME: str = "BioCodeTechPay"

    # Platform PIX receiving key — the actual EVP key registered in BACEN DICT via Asaas.
    # Run `python scripts/check_pix_key.py` to discover the correct value for your Asaas account.
    # Set in Render Dashboard as PLATFORM_PIX_KEY. Falls back to the bundled UUID when absent.
    PLATFORM_PIX_KEY: Optional[str] = None

    # OpenRouter API key — required for the Bio Tech Pay Intelligence chat agent.
    # Set in Render Dashboard as OPENROUTER_API_KEY. Never commit this value.
    OPENROUTER_API_KEY: Optional[str] = None

    # Transactional email via Resend (https://resend.com)
    # CRITICAL: set only via environment variable — never hardcode this value.
    RESEND_API_KEY: Optional[str] = None
    RESEND_FROM_NAME: str = "BioCodeTechPay"
    RESEND_FROM_EMAIL: str = "onboarding@resend.dev"
    # Base URL used to build verification links (e.g. https://payvora-x.onrender.com)
    APP_BASE_URL: str = "http://localhost:8000"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


settings = Settings()
