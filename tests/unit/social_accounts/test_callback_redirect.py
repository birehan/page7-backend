"""OAuth callback redirect always uses configured origin (open-redirect guard)."""

from __future__ import annotations

from app.features.social_accounts.service import (
    build_callback_redirect,
    build_oauth_provider_redirect_url,
    mint_oauth_state,
    normalize_oauth_return_path,
    oauth_return_path_from_state,
)


def test_callback_redirect_uses_configured_origin() -> None:
    url = build_callback_redirect(
        "https://app.pgblank.ai", connected="instagram"
    )
    assert url.startswith("https://app.pgblank.ai/settings/channels")
    assert "connected=instagram" in url


def test_callback_redirect_ignores_client_supplied_host() -> None:
    """Origin argument is config-only; even a hostile-looking string is prefixed as-is
    from settings — never composed from a query param. This test documents that the
    helper has no alternate redirect input."""
    url = build_callback_redirect(
        "http://localhost:3000", error="oauth_failed"
    )
    assert url == "http://localhost:3000/settings/channels?error=oauth_failed"
    assert "evil.example" not in url


def test_callback_redirect_error_takes_precedence() -> None:
    url = build_callback_redirect(
        "http://localhost:3000", connected="instagram", error="denied"
    )
    assert "error=denied" in url
    assert "connected=" not in url


def test_callback_redirect_honours_allowlisted_return_path() -> None:
    url = build_callback_redirect(
        "http://localhost:3000",
        return_path="/onboarding",
        connected="facebook",
    )
    assert url == "http://localhost:3000/onboarding?connected=facebook"


def test_callback_redirect_rejects_unknown_return_path() -> None:
    url = build_callback_redirect(
        "http://localhost:3000",
        return_path="https://evil.example/phish",
        connected="instagram",
    )
    assert url.startswith("http://localhost:3000/settings/channels")
    assert "evil.example" not in url


def test_provider_redirect_embeds_state_on_callback_url() -> None:
    url = build_oauth_provider_redirect_url(
        callback_url="http://localhost:8000/v1/integrations/zernio/callback",
        state="abc+/=xyz",
    )
    assert url.startswith(
        "http://localhost:8000/v1/integrations/zernio/callback?state="
    )
    assert "state=abc" in url


def test_provider_redirect_preserves_existing_query() -> None:
    url = build_oauth_provider_redirect_url(
        callback_url="http://localhost:8000/v1/integrations/zernio/callback?x=1",
        state="s",
    )
    assert url == (
        "http://localhost:8000/v1/integrations/zernio/callback?x=1&state=s"
    )


def test_mint_oauth_state_encodes_return_path() -> None:
    onboarding = mint_oauth_state(return_path="/onboarding")
    channels = mint_oauth_state(return_path="/settings/channels")
    assert onboarding.startswith("o.")
    assert channels.startswith("c.")
    assert oauth_return_path_from_state(onboarding) == "/onboarding"
    assert oauth_return_path_from_state(channels) == "/settings/channels"
    assert oauth_return_path_from_state("legacy-token-without-prefix") == (
        "/settings/channels"
    )
    assert normalize_oauth_return_path("/dashboard") == "/settings/channels"
