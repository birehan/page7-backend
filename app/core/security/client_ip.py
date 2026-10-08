"""Resolve the caller's IP address behind Cloud Run or a load balancer.

`request.client.host` is the proxy, not the user, on Cloud Run, so every per-IP rate
limit and audit row would collapse onto one address. Each trusted proxy appends the
address it saw to the right of `X-Forwarded-For`, so the Nth entry from the right
(N = `TRUSTED_PROXY_HOPS`) is the real client. Entries further left are client-supplied
and must never be trusted. With 0 hops (the default, used in local dev) only the socket
peer is used and the header is ignored.
"""

from __future__ import annotations

import ipaddress

from fastapi import Request

from app.core.config import get_settings


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: Request, *, trusted_hops: int | None = None) -> str | None:
    peer = request.client.host if request.client else None
    hops = get_settings().trusted_proxy_hops if trusted_hops is None else trusted_hops
    if hops <= 0:
        return peer
    entries = [p for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(entries) < hops:
        # Fewer hops than configured: the request did not come through the full proxy
        # chain, so the header cannot be trusted.
        return peer
    return _valid_ip(entries[-hops]) or peer
