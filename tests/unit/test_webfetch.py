"""Unit tests for SSRF IP blocklist and HTML extraction."""

from __future__ import annotations

import pytest

from app.integrations.errors import ProviderUnavailableError
from app.integrations.webfetch.extract import extract_page
from app.integrations.webfetch.fetcher import content_hash_for_pages
from app.integrations.webfetch.ssrf import (
    _pinned_netloc,
    is_blocked_ip,
    resolve_public_ips,
    safe_connect,
)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "0.0.0.0",  # noqa: S104 — blocklist fixture, not a bind
        "::1",
        "fc00::1",
        "fe80::1",
        "224.0.0.1",
        "not-an-ip",
    ],
)
def test_blocked_ip_ranges(ip: str) -> None:
    assert is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2001:4860:4860::8888"])
def test_public_ips_allowed(ip: str) -> None:
    assert is_blocked_ip(ip) is False


def test_pinned_netloc_brackets_ipv6() -> None:
    assert _pinned_netloc("5.252.75.13", None) == "5.252.75.13"
    assert _pinned_netloc("5.252.75.13", 443) == "5.252.75.13:443"
    assert (
        _pinned_netloc("2a02:4780:46:a2b:13c3:655e:d5a3:8a33", None)
        == "[2a02:4780:46:a2b:13c3:655e:d5a3:8a33]"
    )
    assert (
        _pinned_netloc("2a02:4780:46:a2b:13c3:655e:d5a3:8a33", 443)
        == "[2a02:4780:46:a2b:13c3:655e:d5a3:8a33]:443"
    )


def test_resolve_public_ips_prefers_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fake_getaddrinfo(
        host: str, port: object, *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]]:
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2a02:4780:46::1", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("5.252.75.13", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert resolve_public_ips("obeidhospitals.com")[0] == "5.252.75.13"


@pytest.mark.asyncio
async def test_safe_connect_pins_ipv6_with_brackets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unbracketed IPv6 authorities make httpx treat address segments as a port."""
    import socket

    captured: dict[str, object] = {}

    def fake_getaddrinfo(
        host: str, port: object, *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int, int, int]]]:
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2a02:4780:46:a2b:13c3:655e:d5a3:8a33", 0, 0, 0),
            )
        ]

    class _FakeResponse:
        is_redirect = False
        status_code = 200
        content = b"<html></html>"
        headers = {"content-type": "text/html"}

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> _FakeResponse:
            captured["url"] = url
            return _FakeResponse()

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("app.integrations.webfetch.ssrf.httpx.AsyncClient", _FakeClient)

    response = await safe_connect("https://dualstack.example/robots.txt")
    assert response.status_code == 200
    assert captured["url"] == (
        "https://[2a02:4780:46:a2b:13c3:655e:d5a3:8a33]/robots.txt"
    )


@pytest.mark.asyncio
async def test_safe_connect_blocks_loopback() -> None:
    with pytest.raises(ProviderUnavailableError, match="non-public|refusing"):
        await safe_connect("http://127.0.0.1/")


@pytest.mark.asyncio
async def test_safe_connect_blocks_metadata_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fake_getaddrinfo(
        host: str, port: object, *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ProviderUnavailableError, match="non-public|refusing"):
        await safe_connect("http://evil.example/")


@pytest.mark.asyncio
async def test_safe_connect_sends_html_accept_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloudflare challenge pages 403 bare clients; HTML Accept is required."""
    import socket

    captured: dict[str, object] = {}

    def fake_getaddrinfo(
        host: str, port: object, *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    class _FakeResponse:
        is_redirect = False
        status_code = 200
        content = b"<html></html>"
        headers = {"content-type": "text/html"}

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> _FakeResponse:
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return _FakeResponse()

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("app.integrations.webfetch.ssrf.httpx.AsyncClient", _FakeClient)

    response = await safe_connect("https://example.com/")
    assert response.status_code == 200
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert "text/html" in str(headers.get("Accept", ""))
    assert headers.get("User-Agent") == "pgblank-research/1.0"
    assert "Accept-Language" in headers


def test_extract_page_signals() -> None:
    html = """
    <html lang="ar">
      <head>
        <title>Noor Dental</title>
        <meta property="og:title" content="Noor Dental Clinic" />
        <meta property="og:image" content="/logo.png" />
        <meta name="theme-color" content="#0B5D3B" />
        <script type="application/ld+json">
          {"@type":"LocalBusiness","name":"Noor Dental","telephone":"+966500000000"}
        </script>
      </head>
      <body>
        <nav>
          <a href="/about">About us</a>
          <a href="/contact">Contact</a>
          <a href="https://instagram.com/noor">IG</a>
        </nav>
        <main>
          <p>عيادة أسنان في الرياض. Friendly modern care.</p>
          <a href="mailto:hello@noor.sa">email</a>
        </main>
      </body>
    </html>
    """
    page = extract_page(html, page_url="https://noor.sa/")
    assert page.title == "Noor Dental"
    assert page.og["og:title"] == "Noor Dental Clinic"
    assert page.theme_color == "#0B5D3B"
    assert page.logo_url == "https://noor.sa/logo.png"
    assert page.language == "ar"
    assert any(item.get("name") == "Noor Dental" for item in page.json_ld)
    assert "https://instagram.com/noor" in page.social_links
    assert "mailto:hello@noor.sa" in page.contact_links
    assert "https://noor.sa/about" in page.same_site_links
    assert "https://noor.sa/contact" in page.same_site_links
    assert "عيادة" in page.text
    assert "script" not in page.text.lower()


def test_content_hash_stable_and_sensitive() -> None:
    from app.integrations.webfetch.extract import PageExtraction

    a = PageExtraction(url="https://a.sa/about", text="hello")
    b = PageExtraction(url="https://a.sa/", text="home")
    h1 = content_hash_for_pages([a, b])
    h2 = content_hash_for_pages([b, a])
    assert h1 == h2
    h3 = content_hash_for_pages([PageExtraction(url="https://a.sa/", text="changed")])
    assert h1 != h3
