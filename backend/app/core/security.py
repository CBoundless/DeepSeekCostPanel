from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Tuple


PASSWORD_ITERATIONS = 120_000


def hash_password(password: str, salt: str | None = None) -> Tuple[str, str]:
    actual_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        actual_salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    )
    return base64.b64encode(digest).decode("utf-8"), actual_salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    expected_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(expected_hash, password_hash)


def issue_session_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def mask_secret(value: str | None, keep: int = 4) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if len(raw) <= keep:
        return "*" * len(raw)
    return f"{'*' * max(4, len(raw) - keep)}{raw[-keep:]}"
