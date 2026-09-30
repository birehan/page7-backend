"""Shared helpers for brand_approaches lab CLIs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PGBLANK_ROOT = Path("/home/babi/pgblank")
PGBLANK_COMPARE = PGBLANK_ROOT / "tools" / "brand-extract-compare"


def ensure_scripts_on_path() -> None:
    """Allow importing sibling scripts under backend/scripts/."""
    path = str(SCRIPTS_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def normalize_url(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise SystemExit("error: empty URL")
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"error: invalid URL: {raw!r}")
    return parsed.geturl()


def status(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(
    *,
    approach: str,
    url: str,
    started: float,
    result: Mapping[str, Any] | list[Any] | None,
    warnings: list[str] | None = None,
    ok: bool = True,
) -> int:
    """Print the standard lab envelope as JSON on stdout."""
    payload = {
        "approach": approach,
        "url": url,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "result": result,
        "warnings": list(warnings or []),
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


def build_url_parser(
    description: str,
    *,
    extra: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("url", help="Brand website URL")
    if extra is not None:
        extra(parser)
    return parser
