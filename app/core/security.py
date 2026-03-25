"""
Cryptographic utilities and data masking.
Password hashing and JWT creation are authoritative in app.auth.service.
This module exposes only mask_sensitive_data — used by adapters and services
that must not import from the auth layer to avoid circular dependencies.
"""


def mask_sensitive_data(value: str, mask_char: str = "*", visible_chars: int = 4) -> str:
    """
    Sanitizes sensitive information for audit logs, preserving only the trailing
    characters for identification. Constant-length output prevents length inference.
    """
    if not value or len(value) <= visible_chars:
        return mask_char * len(value) if value else ""

    return mask_char * (len(value) - visible_chars) + value[-visible_chars:]
