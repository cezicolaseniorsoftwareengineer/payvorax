"""
Email service — BioCodeTechPay transactional messaging via Resend.

Architecture decision:
  - Resend provides higher deliverability than direct SMTP and a cleaner SDK.
  - API key is read exclusively from environment variable RESEND_API_KEY.
  - When the key is absent (local dev), all sends degrade gracefully to log-only.
  - The key value MUST NOT appear in any source file, log line, or traceback.

Resend docs: https://resend.com/docs/send-with-python
"""

try:
    import resend as _resend
except ImportError:  # pragma: no cover — resend is in requirements.txt for production
    _resend = None  # type: ignore[assignment]

from app.core.config import settings
from app.core.logger import logger


def _configured() -> bool:
    return bool(settings.RESEND_API_KEY) and _resend is not None


def _from_address() -> str:
    return f"{settings.RESEND_FROM_NAME} <{settings.RESEND_FROM_EMAIL}>"


def send_email(to: str, subject: str, html_body: str) -> bool:
    """
    Sends a transactional HTML email via Resend.
    Returns True on success, False on failure or when not configured.
    API key is injected per-call to avoid module-level state leaks.

    Prerequisites in production (Render):
      RESEND_API_KEY   — API key from resend.com/api-keys
      RESEND_FROM_EMAIL — must be from a domain verified in Resend Dashboard
      APP_BASE_URL     — must be https://BioCodeTechPay.onrender.com (never localhost)
    """
    if not _configured():
        logger.warning(
            f"[EMAIL NOT SENT — RESEND_API_KEY not set] to={to} subject={subject}. "
            "Set RESEND_API_KEY in Render environment variables."
        )
        logger.debug(f"[EMAIL BODY PREVIEW]: {html_body[:200]}")
        return False

    try:
        _resend.api_key = settings.RESEND_API_KEY  # injected from env at call time

        _resend.Emails.send({
            "from": _from_address(),
            "to": [to],
            "subject": subject,
            "html": html_body,
        })

        logger.info(f"[EMAIL SENT] to={to} subject={subject}")
        return True

    except Exception as exc:
        # Log class name only — never log the exception message which could
        # contain API key fragments in certain SDK error payloads.
        logger.error(
            f"[EMAIL FAILED] to={to} subject={subject} "
            f"error_type={type(exc).__name__}"
        )
        return False


# ---------------------------------------------------------------------------
# Named message templates
# ---------------------------------------------------------------------------

def send_verification_email(to: str, name: str, token: str) -> bool:
    """Sends the account email-verification link after registration."""
    url = f"{settings.APP_BASE_URL}/auth/verificar-email?token={token}"

    html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="600" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#820AD1 0%,#6d28d9 100%);padding:36px 32px;text-align:center;">
            <h1 style="color:#ffffff;margin:0;font-size:28px;letter-spacing:-0.5px;">BioCodeTechPay</h1>
            <p style="color:#e9d5ff;margin:8px 0 0;font-size:15px;">Verificacao de e-mail</p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:40px 32px;">
            <p style="font-size:16px;color:#1a1a1a;margin:0 0 12px;">Ola, <strong>{name}</strong></p>
            <p style="color:#555;line-height:1.6;margin:0 0 28px;">
              Seu cadastro no <strong>BioCodeTechPay</strong> foi realizado com sucesso.
              Para liberar todas as funcionalidades da sua conta, confirme seu endereco de e-mail clicando no botao abaixo.
            </p>
            <div style="text-align:center;margin:32px 0;">
              <a href="{url}"
                 style="display:inline-block;background:#820AD1;color:#ffffff;text-decoration:none;
                        padding:16px 40px;border-radius:8px;font-weight:700;font-size:16px;
                        letter-spacing:0.2px;">
                Confirmar E-mail
              </a>
            </div>
            <p style="color:#999;font-size:13px;line-height:1.5;">
              Este link e valido por <strong>24 horas</strong>.
              Se voce nao criou uma conta no BioCodeTechPay, ignore este e-mail com seguranca.
            </p>
            <hr style="border:none;border-top:1px solid #eeeeee;margin:28px 0;">
            <p style="color:#bbb;font-size:12px;text-align:center;margin:0;">
              BioCodeTechPay &mdash; Tecnologia financeira com seguranca e transparencia.
            </p>
          </td>
        </tr>

      </table>
    </td></tr></table>
</body>
</html>"""
    return send_email(to, "Confirme seu e-mail — BioCodeTechPay", html)


def send_password_reset_email(to: str, name: str, token: str) -> bool:
    """Sends the password-reset link (legacy link-based flow — kept for compatibility)."""
    url = f"{settings.APP_BASE_URL}/auth/redefinir-senha?token={token}"

    html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="600" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#820AD1 0%,#6d28d9 100%);padding:36px 32px;text-align:center;">
            <h1 style="color:#ffffff;margin:0;font-size:28px;letter-spacing:-0.5px;">BioCodeTechPay</h1>
            <p style="color:#e9d5ff;margin:8px 0 0;font-size:15px;">Redefinicao de senha</p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:40px 32px;">
            <p style="font-size:16px;color:#1a1a1a;margin:0 0 12px;">Ola, <strong>{name}</strong></p>
            <p style="color:#555;line-height:1.6;margin:0 0 28px;">
              Recebemos uma solicitacao para redefinir a senha da sua conta <strong>BioCodeTechPay</strong>.
              Clique no botao abaixo para criar uma nova senha. Se nao foi voce, ignore este e-mail.
            </p>
            <div style="text-align:center;margin:32px 0;">
              <a href="{url}"
                 style="display:inline-block;background:#820AD1;color:#ffffff;text-decoration:none;
                        padding:16px 40px;border-radius:8px;font-weight:700;font-size:16px;">
                Redefinir Senha
              </a>
            </div>
            <p style="color:#999;font-size:13px;line-height:1.5;">
              Este link expira em <strong>1 hora</strong>.
              Por seguranca, nunca compartilhe este link com ninguem.
            </p>
            <hr style="border:none;border-top:1px solid #eeeeee;margin:28px 0;">
            <p style="color:#bbb;font-size:12px;text-align:center;margin:0;">
              BioCodeTechPay &mdash; Tecnologia financeira com seguranca e transparencia.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return send_email(to, "Redefinicao de senha — BioCodeTechPay", html)


def send_temp_password_email(to: str, name: str, temp_password: str) -> bool:
    """
    Sends a temporary password to the user.
    The user must use this temporary password to unlock the reset form and define a new one.
    The temp password is valid for 1 hour and invalidated after first use.
    """
    reset_url = f"{settings.APP_BASE_URL}/redefinir-senha"

    html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="600" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#820AD1 0%,#6d28d9 100%);padding:36px 32px;text-align:center;">
            <h1 style="color:#ffffff;margin:0;font-size:28px;letter-spacing:-0.5px;">BioCodeTechPay</h1>
            <p style="color:#e9d5ff;margin:8px 0 0;font-size:15px;">Recuperacao de senha</p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:40px 32px;">
            <p style="font-size:16px;color:#1a1a1a;margin:0 0 12px;">Ola, <strong>{name}</strong></p>
            <p style="color:#555;line-height:1.6;margin:0 0 24px;">
              Recebemos uma solicitacao de recuperacao de senha para sua conta <strong>BioCodeTechPay</strong>.
              Use a senha temporaria abaixo para acessar a tela de redefinicao e criar uma nova senha.
            </p>

            <!-- Temporary password box -->
            <div style="background:#f3e8ff;border:2px solid #820AD1;border-radius:12px;padding:24px;text-align:center;margin:0 0 28px;">
              <p style="color:#6d28d9;font-size:13px;font-weight:600;margin:0 0 8px;text-transform:uppercase;letter-spacing:1px;">Senha temporaria</p>
              <p style="color:#1a1a1a;font-size:28px;font-weight:700;margin:0;letter-spacing:4px;font-family:monospace;">{temp_password}</p>
              <p style="color:#9ca3af;font-size:12px;margin:8px 0 0;">Valida por 1 hora. Use apenas uma vez.</p>
            </div>

            <div style="text-align:center;margin:0 0 28px;">
              <a href="{reset_url}"
                 style="display:inline-block;background:#820AD1;color:#ffffff;text-decoration:none;
                        padding:16px 40px;border-radius:8px;font-weight:700;font-size:16px;">
                Redefinir minha senha
              </a>
            </div>

            <p style="color:#999;font-size:13px;line-height:1.5;">
              Se voce nao solicitou a recuperacao de senha, ignore este e-mail com seguranca.
              Sua senha atual permanece inalterada.
            </p>
            <hr style="border:none;border-top:1px solid #eeeeee;margin:28px 0;">
            <p style="color:#bbb;font-size:12px;text-align:center;margin:0;">
              BioCodeTechPay &mdash; Tecnologia financeira com seguranca e transparencia.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return send_email(to, "Sua senha temporaria — BioCodeTechPay", html)


def send_notification_email(to: str, name: str, subject: str, body_html: str) -> bool:
    """
    Generic notification to correntistas (transaction alerts, security notices, etc).
    Caller provides pre-rendered HTML body fragment; this wraps it in the standard
    BioCodeTechPay email shell.
    """
    html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="600" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
        <tr>
          <td style="background:linear-gradient(135deg,#820AD1 0%,#6d28d9 100%);padding:36px 32px;text-align:center;">
            <h1 style="color:#ffffff;margin:0;font-size:28px;letter-spacing:-0.5px;">BioCodeTechPay</h1>
          </td>
        </tr>
        <tr>
          <td style="padding:40px 32px;">
            <p style="font-size:16px;color:#1a1a1a;margin:0 0 16px;">Ola, <strong>{name}</strong></p>
            {body_html}
            <hr style="border:none;border-top:1px solid #eeeeee;margin:28px 0;">
            <p style="color:#bbb;font-size:12px;text-align:center;margin:0;">
              BioCodeTechPay &mdash; Tecnologia financeira com seguranca e transparencia.


            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return send_email(to, subject, html)

