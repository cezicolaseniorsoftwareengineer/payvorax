"""
notify_unverified_users.py — Pre-deploy migration for email verification enforcement.

Context:
    The platform now requires email_verified=True to access any protected route.
    Users registered before this enforcement have email_verified=False and will be
    locked out after deploy. This script re-sends (or sends for the first time)
    a verification email to every unverified, active, non-admin account.

    A new cryptographically-secure token is generated and stored regardless of
    whether a previous token exists, ensuring the link is fresh (24h window).

Usage:
    python scripts/notify_unverified_users.py [--dry-run]

Flags:
    --dry-run    Print what would be updated without sending any email or
                 modifying the database. Safe to run multiple times.

Safety invariants:
    - Skips admin accounts (they are managed manually via the admin panel).
    - Skips inactive accounts (is_active=False).
    - Does not modify email_verified — only issues a new token and sends the link.
    - All DB writes are committed individually; a failed email send does not
      roll back the token update (token is still valid, re-run re-sends).
    - Exit code 1 on any unrecoverable error; 0 on clean completion.
    - RESEND_API_KEY absence is treated as a hard failure in production mode
      (not a silent no-op) to prevent silent data loss.
"""

import os
import sys
import argparse
import secrets
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.core.database import SessionLocal
from app.auth.models import User
from app.cards.models import CreditCard  # noqa: F401 — resolve ORM relationship User->CreditCard
from app.core.email_service import send_verification_email
from app.core.config import settings


# ---------------------------------------------------------------------------
# Guard: ensure Resend is configured before touching production data
# ---------------------------------------------------------------------------

def _assert_email_configured(dry_run: bool) -> None:
    if dry_run:
        return
    if not settings.RESEND_API_KEY:
        print(
            "\nERRO: RESEND_API_KEY nao configurado.\n"
            "O script nao enviara emails sem a chave da API.\n"
            "Execute com --dry-run para simular, ou configure RESEND_API_KEY no ambiente.\n"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> None:
    _assert_email_configured(dry_run)

    mode = "[DRY-RUN] " if dry_run else ""
    print(f"=== {mode}NOTIFICAR USUARIOS SEM VERIFICACAO DE EMAIL ===")
    print(f"APP_BASE_URL: {settings.APP_BASE_URL}")
    print()

    db = SessionLocal()

    try:
        unverified = (
            db.query(User)
            .filter(
                User.email_verified == False,
                User.is_active == True,
                User.is_admin == False,
            )
            .order_by(User.name)
            .all()
        )

        total = len(unverified)

        if total == 0:
            print("Nenhum usuario pendente de verificacao encontrado. Nada a fazer.")
            return

        print(f"Usuarios encontrados com email_verified=False: {total}")
        print("-" * 60)

        sent = 0
        failed = 0
        skipped = 0

        for user in unverified:
            print(f"  [{user.id[:8]}] {user.name} <{user.email}>", end=" ... ")

            if dry_run:
                print("SIMULADO (--dry-run)")
                skipped += 1
                continue

            # Generate a fresh token — invalidates any previous link
            new_token = secrets.token_urlsafe(32)
            user.email_verification_token = new_token
            user.email_verification_sent_at = datetime.now(timezone.utc)

            try:
                db.commit()
            except Exception:
                db.rollback()
                print("ERRO ao salvar token no banco")
                traceback.print_exc()
                failed += 1
                continue

            ok = send_verification_email(user.email, user.name, new_token)

            if ok:
                print("ENVIADO")
                sent += 1
            else:
                print("FALHA NO ENVIO (token salvo — pode reenviar)")
                failed += 1

        print()
        print("-" * 60)

        if dry_run:
            print(f"DRY-RUN completo. {total} usuario(s) seriam notificados.")
            print("Remova --dry-run para executar de verdade.")
        else:
            print(f"Enviados : {sent}")
            print(f"Falhas   : {failed}")
            print(f"Total    : {total}")

            if failed > 0:
                print()
                print(
                    "AVISO: Alguns emails falharam. Os tokens foram salvos no banco.\n"
                    "Os usuarios afetados podem solicitar reenvio em /auth/reenviar-verificacao\n"
                    "ou o script pode ser executado novamente."
                )
                sys.exit(1)
            else:
                print()
                print("Todos os emails enviados com sucesso.")
                print(
                    f"Os usuarios tem 24 horas para confirmar o link.\n"
                    "Apos o prazo, o link expira e eles devem solicitar novo envio em:\n"
                    "  POST /auth/reenviar-verificacao  {cpf_cnpj: \"...\"}  (sem autenticacao)"
                )

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Envia verificacao de email para todos os usuarios nao verificados."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista os usuarios que seriam notificados sem enviar emails.",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
