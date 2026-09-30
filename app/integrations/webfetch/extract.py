"""HTML page extraction for brand research (architecture/08 §2.1).

The only module permitted to import `bs4` — see pyproject.toml import-linter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

_SOCIAL_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "twitter.com",
    "www.twitter.com",
    "x.com",
    "www.x.com",
    "facebook.com",
    "www.facebook.com",
    "fb.com",
    "linkedin.com",
    "www.linkedin.com",
    "tiktok.com",
    "www.tiktok.com",
    "youtube.com",
    "www.youtube.com",
    "snapchat.com",
    "www.snapchat.com",
}

_ABOUT_LIKE = re.compile(
    r"(about|contact|pricing|services|من.?نحن|اتصل|خدمات|أسعار)",
    re.IGNORECASE,
)

_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


@dataclass(frozen=True)
class PageExtraction:
    url: str
    title: str | None = None
    text: str = ""
    og: dict[str, str] = field(default_factory=dict)
    json_ld: list[dict[str, Any]] = field(default_factory=list)
    social_links: list[str] = field(default_factory=list)
    contact_links: list[str] = field(default_factory=list)
    logo_url: str | None = None
    theme_color: str | None = None
    language: str | None = None
    same_site_links: list[str] = field(default_factory=list)


def extract_page(html: bytes | str, *, page_url: str) -> PageExtraction:
    """Extract brand-relevant signals from one HTML document."""
    if isinstance(html, bytes):
        text_html = html.decode("utf-8", errors="replace")
    else:
        text_html = html
    soup = BeautifulSoup(text_html, "html.parser")

    # Discover same-site nav links before stripping structural chrome.
    same_site = _same_site_nav_links(soup, page_url)
    social, contact = _links(soup, page_url)
    og = _open_graph(soup)
    json_ld = _json_ld_orgs(soup)
    logo = _logo(soup, page_url, og)
    theme = _theme_color(soup)
    title = _text_or_none(soup.title.string if soup.title else None)

    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()

    body_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    language = _language_from_html_attr(text_html) or _language_from_text(body_text)

    return PageExtraction(
        url=page_url,
        title=title or og.get("og:title"),
        text=body_text[:50_000],
        og=og,
        json_ld=json_ld,
        social_links=social,
        contact_links=contact,
        logo_url=logo,
        theme_color=theme,
        language=language,
        same_site_links=same_site,
    )


def _text_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _open_graph(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        if not isinstance(meta, Tag):
            continue
        prop = meta.get("property") or meta.get("name")
        content = meta.get("content")
        if isinstance(prop, str) and isinstance(content, str) and prop.startswith("og:"):
            out[prop] = content.strip()
    return out


def _json_ld_orgs(soup: BeautifulSoup) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not isinstance(script, Tag) or not script.string:
            continue
        try:
            payload: Any = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        for item in _flatten_ld(payload):
            typ = item.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(t in {"Organization", "LocalBusiness"} for t in types if isinstance(t, str)):
                results.append(item)
    return results


def _flatten_ld(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if "@graph" in payload and isinstance(payload["@graph"], list):
            return [x for x in payload["@graph"] if isinstance(x, dict)]
        return [payload]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _links(soup: BeautifulSoup, page_url: str) -> tuple[list[str], list[str]]:
    social: list[str] = []
    contact: list[str] = []
    seen_social: set[str] = set()
    seen_contact: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href_raw = anchor.get("href")
        if not isinstance(href_raw, str):
            continue
        href = href_raw.strip()
        if href.startswith("mailto:") or href.startswith("tel:"):
            if href not in seen_contact:
                seen_contact.add(href)
                contact.append(href)
            continue
        absolute = urljoin(page_url, href)
        host = (urlparse(absolute).hostname or "").lower()
        if host in _SOCIAL_HOSTS and absolute not in seen_social:
            seen_social.add(absolute)
            social.append(absolute)
    return social, contact


def _rel_includes_icon(value: object) -> bool:
    if isinstance(value, list):
        return any(isinstance(part, str) and "icon" in part.lower() for part in value)
    if isinstance(value, str):
        return "icon" in value.lower()
    return False


def _logo(soup: BeautifulSoup, page_url: str, og: dict[str, str]) -> str | None:
    if "og:image" in og:
        return urljoin(page_url, og["og:image"])
    for link in soup.find_all("link"):
        if not isinstance(link, Tag):
            continue
        if not _rel_includes_icon(link.get("rel")):
            continue
        href = link.get("href")
        if isinstance(href, str) and href.strip():
            return urljoin(page_url, href.strip())
    return None


def _theme_color(soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", attrs={"name": "theme-color"})
    if isinstance(meta, Tag):
        content = meta.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _language_from_html_attr(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    html_tag = soup.find("html")
    if isinstance(html_tag, Tag):
        lang = html_tag.get("lang")
        if isinstance(lang, str) and lang.strip():
            return lang.strip().lower()[:8]
    return None


def _language_from_text(text: str) -> str | None:
    if not text:
        return None
    arabic = len(_ARABIC_RE.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if arabic == 0 and latin == 0:
        return None
    return "ar" if arabic >= latin else "en"


def _same_site_nav_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Discover about/contact/pricing-shaped same-site links from nav/footer."""
    base = urlparse(page_url)
    base_host = (base.hostname or "").lower()
    candidates: list[str] = []
    seen: set[str] = set()
    scopes: list[Tag | BeautifulSoup] = list(soup.find_all(["nav", "footer", "header"]))
    if not scopes:
        scopes = [soup]
    for scope in scopes:
        if not isinstance(scope, Tag) and scope is not soup:
            continue
        for anchor in scope.find_all("a", href=True):
            if not isinstance(anchor, Tag):
                continue
            href_raw = anchor.get("href")
            if not isinstance(href_raw, str):
                continue
            absolute = urljoin(page_url, href_raw.strip())
            parsed = urlparse(absolute)
            if (parsed.hostname or "").lower() != base_host:
                continue
            label = (anchor.get_text(" ", strip=True) or "") + " " + (parsed.path or "")
            if not _ABOUT_LIKE.search(label):
                continue
            if not parsed.path or parsed.path == "/":
                continue
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(normalized)
    return candidates[:12]
