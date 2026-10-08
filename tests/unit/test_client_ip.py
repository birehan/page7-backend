from __future__ import annotations

from starlette.requests import Request

from app.core.security.client_ip import client_ip


def _request(peer: str | None, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


def test_zero_hops_ignores_forwarded_header() -> None:
    assert client_ip(_request("10.0.0.1", "1.2.3.4"), trusted_hops=0) == "10.0.0.1"


def test_one_hop_uses_rightmost_entry() -> None:
    # Cloud Run appends the real client to the right of whatever the client sent.
    assert client_ip(_request("10.0.0.1", "6.6.6.6, 203.0.113.9"), trusted_hops=1) == "203.0.113.9"


def test_spoofed_leftmost_entry_is_never_used() -> None:
    request = _request("10.0.0.1", "1.1.1.1, 2.2.2.2, 198.51.100.7")
    assert client_ip(request, trusted_hops=1) == "198.51.100.7"


def test_two_hops_skips_the_load_balancer_entry() -> None:
    request = _request("10.0.0.1", "198.51.100.7, 35.191.0.1")
    assert client_ip(request, trusted_hops=2) == "198.51.100.7"


def test_header_shorter_than_hops_falls_back_to_peer() -> None:
    assert client_ip(_request("10.0.0.1", "198.51.100.7"), trusted_hops=2) == "10.0.0.1"
    assert client_ip(_request("10.0.0.1"), trusted_hops=1) == "10.0.0.1"


def test_garbage_entry_falls_back_to_peer() -> None:
    assert client_ip(_request("10.0.0.1", "1.1.1.1, not-an-ip"), trusted_hops=1) == "10.0.0.1"


def test_ipv6_entry_is_normalised() -> None:
    assert client_ip(_request("10.0.0.1", "x, 2001:DB8::1"), trusted_hops=1) == "2001:db8::1"


def test_no_client_and_no_header_is_none() -> None:
    assert client_ip(_request(None), trusted_hops=1) is None
