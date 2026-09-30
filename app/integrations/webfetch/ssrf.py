"""SSRF-safe HTTP connect (architecture/05 §7).

Resolve DNS once, validate every resulting IP against the private/link-local/
loopback/multicast/metadata blocklist, then connect to the validated IP
directly with the original hostname as Host and SNI — never hand the hostname
back for a second, independent resolution (the DNS-rebinding hole).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from app.integrations.errors import ProviderTimeoutError, ProviderUnavailableError

_MAX_REDIRECTS = 3
_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_TIMEOUT = 30.0

# Browser-like Accept is required: several CDNs (Cloudflare challenge pages) return
# 403 to clients that send only User-Agent without an HTML Accept header.
_RESEARCH_HEADERS = {
    "User-Agent": "pgblank-research/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

# Explicit cloud-metadata ranges beyond what ipaddress flags as link-local.
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("fd00:ec2::/32"),  # AWS IMDSv2 IPv6
)


@dataclass(frozen=True)
class SafeResponse:
    """One hop's final (non-redirect) response after SSRF-safe connect."""

    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    content_type: str


def is_blocked_ip(ip: str) -> bool:
    """Return True when `ip` must never be connected to from this process."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    return any(addr in network for network in _BLOCKED_NETWORKS)


def resolve_public_ips(hostname: str) -> list[str]:
    """Resolve `hostname` and return only public IPs; raise if none remain.

    IPv4 is preferred when both families are present — string-sorted AAAA
    records often sort ahead of A records and broke URL pinning before
    brackets were applied.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ProviderUnavailableError(f"DNS resolution failed for {hostname}") from exc
    ips = sorted({str(info[4][0]) for info in infos})
    public = [ip for ip in ips if not is_blocked_ip(ip)]
    if not public:
        raise ProviderUnavailableError(f"refusing non-public host: {hostname}")
    v4 = [ip for ip in public if ":" not in ip]
    v6 = [ip for ip in public if ":" in ip]
    return [*v4, *v6]


def _pinned_netloc(ip: str, port: int | None) -> str:
    """Build ``host`` or ``[ipv6]:port`` for an IP-literal URL authority."""
    host = f"[{ip}]" if ":" in ip else ip
    if port is not None:
        return f"{host}:{port}"
    return host


def _absolute_redirect(current: str, location: str, hostname: str) -> str:
    if "://" in location:
        return location
    parsed = urlparse(current)
    base = f"{parsed.scheme}://{hostname}"
    if parsed.port:
        base = f"{parsed.scheme}://{hostname}:{parsed.port}"
    return urljoin(base + "/", location)


async def safe_connect(
    url: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    timeout_seconds: float = _DEFAULT_TIMEOUT,
    max_redirects: int = _MAX_REDIRECTS,
) -> SafeResponse:
    """Fetch `url` with resolve-validate-pin on every hop, including redirects.

    TLS uses the original hostname as SNI (`extensions.sni_hostname`) while the
    TCP connection targets the validated IP — without SNI, certificate
    verification fails against the IP literal for essentially every real site.
    """
    current = url
    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ProviderUnavailableError("only http(s) URLs are allowed")
        hostname = parsed.hostname
        ips = resolve_public_ips(hostname)
        pinned_url = current.replace(
            f"{parsed.scheme}://{parsed.netloc}",
            f"{parsed.scheme}://{_pinned_netloc(ips[0], parsed.port)}",
            1,
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    pinned_url,
                    headers={"Host": hostname, **_RESEARCH_HEADERS},
                    extensions={"sni_hostname": hostname},
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"timed out fetching {hostname}") from exc
        except httpx.InvalidURL as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise ProviderUnavailableError("redirect without Location")
            current = _absolute_redirect(current, location, hostname)
            continue

        if response.status_code >= 400:
            raise ProviderUnavailableError(f"upstream returned {response.status_code}")
        body = response.content
        if len(body) > max_bytes:
            raise ProviderUnavailableError("upstream response exceeds size limit")
        content_type = response.headers.get("content-type", "")
        return SafeResponse(
            url=current,
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items()},
            content=body,
            content_type=content_type,
        )

    raise ProviderUnavailableError("too many redirects")
