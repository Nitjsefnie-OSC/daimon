"""Shared Fernet helpers for encrypted connections.

"""

from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet


def build_multifernet(keys: tuple[str, ...]) -> MultiFernet:
    """Construct MultiFernet from a tuple of base64-urlsafe 32-byte keys.

    First key is used for encryption; all keys are tried for decryption (forward
    compatible with rotation). Raises ValueError at construction if any key is
    not a valid Fernet key — surfaces config errors at boot rather than on first
    OAuth callback (Pitfall 2 in RESEARCH.md).
    """
    if not keys:
        raise ValueError(
            "settings.crypto.keys is empty — at least one Fernet key is required "
            "for credential and agent environment encryption; configure DAIMON_CRYPTO__KEYS. "
            "Generate one with `cryptography.fernet.Fernet.generate_key()`."
        )
    return MultiFernet([Fernet(k.encode("utf-8")) for k in keys])


def encrypt_token(fernet: MultiFernet, plaintext: str) -> bytes:
    return fernet.encrypt(plaintext.encode("utf-8"))


def decrypt_token(fernet: MultiFernet, ciphertext: bytes | memoryview) -> str:
    # asyncpg may return BYTEA as memoryview; Fernet wants bytes (Pitfall 1).
    return fernet.decrypt(bytes(ciphertext)).decode("utf-8")
