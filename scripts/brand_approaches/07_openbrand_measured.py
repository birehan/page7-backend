#!/usr/bin/env python3
"""Approach 07: OpenBrand measured logos/colors (lab, no LLM).

Shells out to ``_openbrand_runner.ts``, which imports the OpenBrand adapter from
``/home/babi/pgblank/tools/brand-extract-compare`` (network-only; no Firecrawl).

Needs:
  - ``/home/babi/pgblank`` with ``pnpm install`` (openbrand under tools/)
  - ``tsx`` available via that package (``pnpm --filter @pgblank/brand-extract-compare exec tsx``)
  - outbound HTTPS

Usage (from backend/):

    uv run python scripts/brand_approaches/07_openbrand_measured.py https://stripe.com
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, cast

from _common import (
    PGBLANK_COMPARE,
    PGBLANK_ROOT,
    build_url_parser,
    emit,
    normalize_url,
    status,
)

_RUNNER = Path(__file__).resolve().parent / "_openbrand_runner.ts"


async def _run_openbrand(url: str) -> dict[str, Any]:
    if not PGBLANK_COMPARE.is_dir():
        return {
            "ok": False,
            "error": f"missing brand-extract-compare at {PGBLANK_COMPARE}",
            "warnings": [f"missing {PGBLANK_COMPARE}"],
        }
    if not _RUNNER.is_file():
        return {
            "ok": False,
            "error": f"missing runner {_RUNNER}",
            "warnings": [f"missing {_RUNNER}"],
        }

    proc = await asyncio.create_subprocess_exec(
        "pnpm",
        "--filter",
        "@pgblank/brand-extract-compare",
        "exec",
        "tsx",
        str(_RUNNER),
        url,
        cwd=str(PGBLANK_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    text = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace")
    if not text:
        return {
            "ok": False,
            "error": err[-800:] or f"exit {proc.returncode}",
            "warnings": ["openbrand produced empty stdout"],
            "exit_code": proc.returncode,
        }
    try:
        payload = cast(dict[str, Any], json.loads(text))
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            payload = cast(dict[str, Any], json.loads(text[start : end + 1]))
        else:
            return {
                "ok": False,
                "error": err[-400:] or text[-400:],
                "warnings": ["openbrand bad JSON"],
                "raw_stdout": text[-500:],
            }
    payload["stderr_tail"] = err[-400:] if err else None
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = build_url_parser("OpenBrand measured logos/colors (lab)")
    args = parser.parse_args(argv)
    url = normalize_url(args.url)

    status(f"[07_openbrand_measured] {url}")
    started = time.perf_counter()
    result = asyncio.run(_run_openbrand(url))
    warnings: list[str] = []
    if result.get("error"):
        warnings.append(str(result["error"]))
    for w in result.get("warnings") or []:
        warnings.append(str(w))
    ok = bool(result.get("ok"))
    return emit(
        approach="07_openbrand_measured",
        url=url,
        started=started,
        result=result,
        warnings=warnings,
        ok=ok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
