from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _load_fernet_key() -> bytes:
    configured = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if configured:
        return configured.encode("utf-8")

    app_env = (os.getenv("APP_ENV") or "development").strip().lower()
    if app_env == "production":
        raise RuntimeError("APP_ENCRYPTION_KEY must be set in production environment")

    # Development fallback key. Set APP_ENCRYPTION_KEY in production.
    material = os.getenv("APP_ENCRYPTION_PASSPHRASE", "local-dev-only-key").encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(material).digest())


def _get_fernet() -> Fernet:
    return Fernet(_load_fernet_key())


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _get_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    clean = str(value)
    if len(clean) <= 4:
        return "*" * len(clean)
    middle = "*" * max(len(clean) - 4, 1)
    return f"{clean[:2]}{middle}{clean[-2:]}"
