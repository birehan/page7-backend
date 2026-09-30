from __future__ import annotations

import asyncio
import base64
import os
from functools import partial

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_hasher = PasswordHasher()

# A fixed, valid Argon2id hash of a value nobody could ever supply as a real
# password. Verifying against this on a login attempt for an email that
# doesn't exist gives the "no such user" branch the same Argon2 cost as
# "wrong password" — architecture/05 §1's enumeration-safety rule only holds
# if the hasher genuinely runs on both paths, not just the message shown.
DUMMY_PASSWORD_HASH = _hasher.hash("not-a-real-password-e3f9c2b7a1d84f6c")


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    try:
        _hasher.verify(password_hash, plain)
    except VerifyMismatchError:
        return False
    return True


async def hash_password_async(plain: str) -> str:
    """Argon2id is deliberately slow/memory-hard — run it off the event loop
    so one hash/verify doesn't block every other in-flight request (concurrent
    login/signup would otherwise serialize behind it)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, hash_password, plain)


async def verify_password_async(password_hash: str, plain: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(verify_password, password_hash, plain))


def _resolve_key(key_id: str) -> bytes:
    """`.env.example` generates each key with `secrets.token_urlsafe(32)` — a
    base64url string with padding stripped, decoding to exactly 32 raw bytes
    (AES-256). Padding is re-added before decoding since `token_urlsafe`
    doesn't emit it.
    """
    secret = get_settings().app_encryption_key.get(key_id)
    if secret is None:
        raise KeyError(f"no APP_ENCRYPTION_KEY__{key_id} configured")
    raw = secret.get_secret_value()
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded)


def encrypt_secret(plaintext: bytes) -> tuple[bytes, str]:
    """AES-256-GCM under the current key (`APP_ENCRYPTION_KEY_CURRENT`).
    Returns `(nonce || ciphertext, key_id)` — the caller stores both; `key_id`
    says which key decrypts this row and is never assumed to be the current
    one at read time (architecture/05 §4's rotation scheme).
    """
    key_id = get_settings().app_encryption_key_current
    if key_id is None:
        raise RuntimeError("APP_ENCRYPTION_KEY_CURRENT is not configured")
    nonce = os.urandom(12)
    ciphertext = AESGCM(_resolve_key(key_id)).encrypt(nonce, plaintext, None)
    return nonce + ciphertext, key_id


def decrypt_secret(ciphertext: bytes, *, key_id: str) -> bytes:
    nonce, body = ciphertext[:12], ciphertext[12:]
    return AESGCM(_resolve_key(key_id)).decrypt(nonce, body, None)
