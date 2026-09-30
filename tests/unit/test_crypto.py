from __future__ import annotations

import secrets

import pytest
from cryptography.exceptions import InvalidTag

from app.core.config import Environment, Settings
from app.core.security import crypto


@pytest.fixture
def two_encryption_keys(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(
        app_env=Environment.DEVELOPMENT,
        app_encryption_key={"k1": secrets.token_urlsafe(32), "k2": secrets.token_urlsafe(32)},
        app_encryption_key_current="k1",
        _env_file=None,
    )
    monkeypatch.setattr(crypto, "get_settings", lambda: settings)
    return settings


def test_hash_and_verify_password_round_trip() -> None:
    hashed = crypto.hash_password("correct horse battery staple")
    assert crypto.verify_password(hashed, "correct horse battery staple") is True


def test_verify_password_rejects_a_wrong_password() -> None:
    hashed = crypto.hash_password("correct horse battery staple")
    assert crypto.verify_password(hashed, "wrong password") is False


def test_dummy_password_hash_never_verifies() -> None:
    # architecture/05 §1: the login flow verifies against this fixed hash on a
    # no-such-user lookup miss, so "wrong password" and "no such user" cost the
    # same Argon2 verify. It must never itself accept a real-looking password.
    assert crypto.verify_password(crypto.DUMMY_PASSWORD_HASH, "anything") is False


@pytest.mark.asyncio
async def test_hash_password_async_round_trips_with_sync_verify() -> None:
    # Argon2 is deliberately slow/memory-hard — the async wrapper must run it
    # off the event loop (via run_in_executor), not just await a coroutine
    # that still blocks. Round-trip against the sync verify to prove the
    # threadpool path produces a real, verifiable Argon2id hash.
    hashed = await crypto.hash_password_async("correct horse battery staple")
    assert crypto.verify_password(hashed, "correct horse battery staple") is True


@pytest.mark.asyncio
async def test_verify_password_async_accepts_correct_and_rejects_wrong() -> None:
    hashed = crypto.hash_password("correct horse battery staple")
    assert await crypto.verify_password_async(hashed, "correct horse battery staple") is True
    assert await crypto.verify_password_async(hashed, "wrong password") is False


@pytest.mark.asyncio
async def test_verify_password_async_runs_off_the_event_loop_thread() -> None:
    # Regression guard for the fixed perf issue: the whole point of the
    # threadpool wrapper is that Argon2 (deliberately slow/memory-hard) never
    # runs directly on the event loop thread. Assert this structurally
    # (which thread actually ran it) rather than by timing, which is flaky
    # under CI/host load and doesn't actually prove offload happened.
    import threading

    hashed = crypto.hash_password("correct horse battery staple")
    event_loop_thread = threading.current_thread()
    seen_thread: threading.Thread | None = None

    original_verify = crypto.verify_password

    def spy(password_hash: str, plain: str) -> bool:
        nonlocal seen_thread
        seen_thread = threading.current_thread()
        return original_verify(password_hash, plain)

    import unittest.mock

    with unittest.mock.patch.object(crypto, "verify_password", spy):
        result = await crypto.verify_password_async(hashed, "correct horse battery staple")

    assert result is True
    assert seen_thread is not None
    assert seen_thread is not event_loop_thread


def test_encrypt_and_decrypt_secret_round_trip(two_encryption_keys: Settings) -> None:
    ciphertext, key_id = crypto.encrypt_secret(b"a totp secret")

    assert key_id == "k1"
    assert ciphertext != b"a totp secret"
    assert crypto.decrypt_secret(ciphertext, key_id=key_id) == b"a totp secret"


def test_decrypt_secret_fails_under_the_wrong_key(two_encryption_keys: Settings) -> None:
    ciphertext, key_id = crypto.encrypt_secret(b"a totp secret")
    assert key_id == "k1"

    with pytest.raises(InvalidTag):
        crypto.decrypt_secret(ciphertext, key_id="k2")


def test_encrypt_secret_requires_a_current_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear any ambient env vars so Settings() has no key alias configured.
    monkeypatch.delenv("APP_ENCRYPTION_KEY_CURRENT", raising=False)
    monkeypatch.delenv("APP_ENCRYPTION_KEY__k1", raising=False)
    settings = Settings(app_env=Environment.DEVELOPMENT, _env_file=None)
    monkeypatch.setattr(crypto, "get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="APP_ENCRYPTION_KEY_CURRENT"):
        crypto.encrypt_secret(b"a totp secret")
