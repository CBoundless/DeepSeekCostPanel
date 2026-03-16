from __future__ import annotations

from cryptography.fernet import Fernet

from .settings import load_or_create_master_key


class SecretCipher:
    def __init__(self) -> None:
        self._fernet = Fernet(load_or_create_master_key())

    def encrypt(self, value: str | None) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        return self._fernet.encrypt(raw.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str | None) -> str:
        raw = (token or "").strip()
        if not raw:
            return ""
        return self._fernet.decrypt(raw.encode("utf-8")).decode("utf-8")


secret_cipher = SecretCipher()
