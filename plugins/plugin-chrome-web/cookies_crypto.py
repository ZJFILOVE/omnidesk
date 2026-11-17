"""Chromium cookie decryption scope and fail-closed decryption boundary."""

from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Callable
from typing import Any

CookieDecryptor = Callable[[str, str, bytes], str | None]


def has_cryptography() -> bool:
    """Return True if optional `cryptography` package is available."""
    return importlib.util.find_spec("cryptography") is not None


def decryption_scope_summary() -> dict[str, Any]:
    """Describe supported vs unsupported Chromium cookie decryption modes."""
    has_crypto = has_cryptography()
    return {
        "plaintext_sqlite": True,
        "linux_v10_peanuts": has_crypto,
        "linux_v11_keyring": False,
        "windows_dpapi": False,
        "macos_keychain": False,
        "chrome_v20_app_bound": False,
        "cryptography_installed": has_crypto,
    }


def encryption_scheme(blob: bytes) -> str:
    """Identify Chromium encrypted_value prefix without exposing bytes."""
    if blob.startswith(b"v10"):
        return "v10"
    if blob.startswith(b"v11"):
        return "v11"
    if blob.startswith(b"v20"):
        return "v20"
    if blob.startswith(b"\x01\x00\x00\x00"):
        return "dpapi"
    return "unknown"


def _decrypt_v10_linux(blob: bytes, host_key: str, *, db_version: int) -> str | None:
    if not has_cryptography() or len(blob) <= 3:
        return None
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return None
    ciphertext = blob[3:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        return None
    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        return None
    if db_version >= 24 and len(plaintext) >= 32:
        host_digest = hashlib.sha256(host_key.encode("utf-8")).digest()
        if plaintext[:32] == host_digest:
            plaintext = plaintext[32:]
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        return None


def decrypt_cookie_value(
    *,
    plain_value: str,
    encrypted_blob: bytes,
    host_key: str,
    name: str,
    db_version: int,
    decryptor: CookieDecryptor | None,
) -> tuple[str | None, str | None]:
    """Return `(decrypted_value, failure_scheme)` without ever fabricating plaintext."""
    if not encrypted_blob or plain_value:
        return plain_value, None
    scheme = encryption_scheme(encrypted_blob)
    if decryptor is not None:
        custom = decryptor(host_key, name, encrypted_blob)
        return (custom, None) if isinstance(custom, str) else (None, scheme)
    if scheme == "v10":
        decrypted = _decrypt_v10_linux(encrypted_blob, host_key, db_version=db_version)
        return (decrypted, None) if decrypted is not None else (None, "v10")
    return None, scheme
