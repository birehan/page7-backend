#!/usr/bin/env python3
"""Evaluate fast_brand_extract on 10 Saudi sites (completeness + timing).

Runs the production fast path against a fixed set of brands that are *not*
in bakeoff_10_sites_brand_extract.py, then reports:

- which of the 10 extraction fields landed (and which are missing)
- simple quality checks (empty lists, bad dialect/lang/color/logo shapes)
- per-site elapsed time + average

Usage (from backend/):

    uv run python scripts/eval_fast_brand_extract_10.py
    uv run python scripts/eval_fast_brand_extract_10.py --concurrency 2
    uv run python scripts/eval_fast_brand_extract_10.py --out scripts/out
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.features.brand_research.fast_extract import fast_extract
from app.features.brand_research.ports import EXTRACTION_FIELD_NAMES

# Distinct from bakeoff_10_sites_brand_extract.py. Pre-probed for curl 200 +
# usable HTML (many large Saudi retail/air sites return 403/timeout to curl).
SITES: tuple[tuple[str, str], ...] = (
    ("Alinma Bank", "https://www.alinma.com/"),
    ("Danube", "https://www.danube.sa/"),
    ("Tamara", "https://www.tamara.co/"),
    ("Tabby", "https://www.tabby.ai/"),
    ("Mrsool", "https://www.mrsool.co/"),
    ("Sary", "https://www.sary.com/"),
    ("Maaden", "https://www.maaden.com.sa/"),
    ("STC", "https://www.stc.com.sa/"),
    ("Tamimi Markets", "https://www.tamimimarkets.com/"),
    ("Almarai", "https://www.almarai.com/"),
)

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{3}([0-9A-Fa-f]{3})?$")
_VALID_DIALECTS = frozenset({"gulf", "msa"})
_VALID_LANGS = frozenset({"ar", "en"})


@dataclass
class FieldEval:
    present: bool
    confidence: float | None = None
    value_preview: Any = None
    issues: list[str] = field(default_factory=list)


@dataclass
class SiteEval:
    site_name: str
    url: str
    status: str
    elapsed_seconds: float | None
    timing: dict[str, Any] = field(default_factory=dict)
    fields_present: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)
    field_count: int = 0
    quality_issues: list[str] = field(default_factory=list)
    field_detail: dict[str, FieldEval] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    raw_fields: dict[str, Any] = field(default_factory=dict)


def _preview(value: Any, *, limit: int = 120) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1] + "…"
    if isinstance(value, list):
        if not value:
            return []
        shown = value[:5]
        if len(value) > 5:
            return [*shown, f"…(+{len(value) - 5})"]
        return shown
    return value


def _filled(value: Any) -> bool:
    return value not in (None, "", [], {})


def _eval_field(name: str, payload: dict[str, Any] | None) -> FieldEval:
    if not payload or not isinstance(payload, dict):
        return FieldEval(present=False, issues=["missing"])

    value = payload.get("value")
    conf_raw = payload.get("confidence")
    conf = float(conf_raw) if isinstance(conf_raw, (int, float)) else None
    issues: list[str] = []

    if not _filled(value):
        return FieldEval(
            present=False,
            confidence=conf,
            value_preview=value,
            issues=["empty_value"],
        )

    if name in {"voice_adjectives", "do_list", "dont_list", "banned_claims",
                "pillars_suggested", "competitors_suggested"}:
        if not isinstance(value, list):
            issues.append("expected_list")
        elif len(value) == 0:
            issues.append("empty_list")
        elif name == "voice_adjectives" and len(value) < 2:
            issues.append("too_few_items")

    elif name == "colors":
        if not isinstance(value, list):
            issues.append("expected_list")
        else:
            bad = [c for c in value if not isinstance(c, str) or not _HEX_RE.match(c)]
            if bad:
                issues.append(f"non_hex_colors:{len(bad)}")
            if len(value) == 0:
                issues.append("empty_list")

    elif name == "dialect":
        if not isinstance(value, str) or value not in _VALID_DIALECTS:
            issues.append(f"invalid_dialect:{value!r}")

    elif name == "languages":
        if not isinstance(value, list):
            issues.append("expected_list")
        else:
            bad = [x for x in value if x not in _VALID_LANGS]
            if bad:
                issues.append(f"invalid_langs:{bad}")
            if not value:
                issues.append("empty_list")

    elif name == "logo_url":
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            issues.append("logo_not_http_url")

    return FieldEval(
        present=True,
        confidence=conf,
        value_preview=_preview(value),
        issues=issues,
    )


def _evaluate_result(site: str, url: str, raw: dict[str, Any]) -> SiteEval:
    status = str(raw.get("status") or "failed")
    fields_raw = dict(raw.get("fields") or {})
    detail: dict[str, FieldEval] = {}
    present: list[str] = []
    missing: list[str] = []
    quality: list[str] = []

    for name in EXTRACTION_FIELD_NAMES:
        fe = _eval_field(name, fields_raw.get(name))
        detail[name] = fe
        if fe.present:
            present.append(name)
            for issue in fe.issues:
                quality.append(f"{name}:{issue}")
        else:
            missing.append(name)
            for issue in fe.issues:
                quality.append(f"{name}:{issue}")

    elapsed = raw.get("elapsed_seconds")
    return SiteEval(
        site_name=site,
        url=url,
        status=status,
        elapsed_seconds=float(elapsed) if elapsed is not None else None,
        timing=dict(raw.get("timing") or {}),
        fields_present=present,
        fields_missing=missing,
        field_count=len(present),
        quality_issues=quality,
        field_detail=detail,
        warnings=list(raw.get("warnings") or []),
        error=raw.get("error") if isinstance(raw.get("error"), dict) else None,
        raw_fields=fields_raw,
    )


async def _run_one(
    *,
    site: str,
    url: str,
    sem: asyncio.Semaphore,
) -> SiteEval:
    async with sem:
        print(f"→ {site}  {url}", file=sys.stderr, flush=True)
        t0 = time.perf_counter()
        try:
            raw = await fast_extract(url, brand_name=site)
        except Exception as exc:  # noqa: BLE001 — eval harness must not abort the run
            elapsed = round(time.perf_counter() - t0, 3)
            print(f"✗ {site}  exception after {elapsed}s: {exc}", file=sys.stderr, flush=True)
            return SiteEval(
                site_name=site,
                url=url,
                status="failed",
                elapsed_seconds=elapsed,
                fields_missing=list(EXTRACTION_FIELD_NAMES),
                field_count=0,
                quality_issues=["exception"],
                error={"code": "EXCEPTION", "message": str(exc)},
            )
        row = _evaluate_result(site, url, raw)
        print(
            f"✓ {site}  status={row.status}  "
            f"fields={row.field_count}/{len(EXTRACTION_FIELD_NAMES)}  "
            f"missing={row.fields_missing or '—'}  "
            f"t={row.elapsed_seconds}s",
            file=sys.stderr,
            flush=True,
        )
        return row


def _site_to_json(row: SiteEval) -> dict[str, Any]:
    payload = asdict(row)
    # Keep report compact: drop bulky raw_fields from summary consumers if needed,
    # but retain them in JSON for inspection.
    return payload


def _print_report(rows: list[SiteEval]) -> None:
    n = len(EXTRACTION_FIELD_NAMES)
    times = [r.elapsed_seconds for r in rows if r.elapsed_seconds is not None]
    avg_t = round(sum(times) / len(times), 3) if times else None
    ok = sum(1 for r in rows if r.status in {"succeeded", "partial"})
    full = sum(1 for r in rows if r.field_count == n)
    avg_fields = round(sum(r.field_count for r in rows) / len(rows), 2) if rows else 0.0

    # Field fill rates across the cohort
    fill: dict[str, int] = {name: 0 for name in EXTRACTION_FIELD_NAMES}
    for row in rows:
        for name in row.fields_present:
            fill[name] += 1

    print("\n" + "=" * 72)
    print("fast_brand_extract — 10-site evaluation")
    print("=" * 72)
    print(
        f"sites={len(rows)}  ok={ok}/{len(rows)}  "
        f"full_10/10={full}/{len(rows)}  "
        f"avg_fields={avg_fields}/{n}  "
        f"avg_time={avg_t}s"
    )
    print()
    print(f"{'Site':<20} {'Status':<10} {'Fields':>7} {'Time':>8}  Missing")
    print("-" * 72)
    for row in rows:
        miss = ",".join(row.fields_missing) if row.fields_missing else "—"
        t = f"{row.elapsed_seconds:.1f}s" if row.elapsed_seconds is not None else "?"
        print(
            f"{row.site_name:<20} {row.status:<10} "
            f"{row.field_count:>3}/{n:<3} {t:>8}  {miss}"
        )

    print()
    print("Field fill rate (present / sites):")
    for name in EXTRACTION_FIELD_NAMES:
        pct = round(100.0 * fill[name] / len(rows), 1) if rows else 0.0
        bar = "█" * fill[name] + "·" * (len(rows) - fill[name])
        print(f"  {name:<24} {fill[name]:>2}/{len(rows)}  {pct:>5.1f}%  {bar}")

    issue_counts: dict[str, int] = {}
    for row in rows:
        for issue in row.quality_issues:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    if issue_counts:
        print()
        print("Quality / missing issues (count across sites):")
        for issue, count in sorted(issue_counts.items(), key=lambda x: (-x[1], x[0])):
            print(f"  {count:>2}×  {issue}")

    print()
    print("Timing:")
    for row in rows:
        total = row.elapsed_seconds
        fetch = row.timing.get("fetch_homepage_seconds")
        llm = row.timing.get("llm_seconds")
        print(
            f"  {row.site_name:<20} total={total}s  fetch={fetch}s  llm={llm}s"
        )
    if avg_t is not None:
        print(f"  {'AVERAGE':<20} total={avg_t}s")
    print("=" * 72)


async def _amain(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    await asyncio.to_thread(out_dir.mkdir, True, True)  # parents=True, exist_ok=True
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    sem = asyncio.Semaphore(max(1, args.concurrency))

    rows = await asyncio.gather(
        *[_run_one(site=name, url=url, sem=sem) for name, url in SITES]
    )
    rows_list = list(rows)

    report = {
        "generated_at": stamp,
        "approach": "fast_curl_llm_v2",
        "sites": [_site_to_json(r) for r in rows_list],
        "summary": {
            "site_count": len(rows_list),
            "ok_count": sum(1 for r in rows_list if r.status in {"succeeded", "partial"}),
            "full_field_count": sum(
                1 for r in rows_list if r.field_count == len(EXTRACTION_FIELD_NAMES)
            ),
            "avg_fields": round(
                sum(r.field_count for r in rows_list) / len(rows_list), 2
            ),
            "avg_elapsed_seconds": (
                round(
                    sum(r.elapsed_seconds for r in rows_list if r.elapsed_seconds is not None)
                    / max(1, sum(1 for r in rows_list if r.elapsed_seconds is not None)),
                    3,
                )
                if any(r.elapsed_seconds is not None for r in rows_list)
                else None
            ),
            "field_fill_rates": {
                name: sum(1 for r in rows_list if name in r.fields_present)
                for name in EXTRACTION_FIELD_NAMES
            },
        },
    }

    json_path = out_dir / f"fast_brand_eval_10_{stamp}.json"
    await asyncio.to_thread(
        json_path.write_text, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"\nwrote {json_path}", file=sys.stderr, flush=True)

    _print_report(rows_list)
    summary = report["summary"]
    assert isinstance(summary, dict)
    ok_count = summary["ok_count"]
    assert isinstance(ok_count, int)
    return 0 if ok_count == len(rows_list) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate fast_brand_extract completeness + timing on 10 sites"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Parallel extractions (default 2; keep low to avoid LLM rate limits)",
    )
    parser.add_argument(
        "--out",
        default="scripts/out",
        help="Directory for JSON report (default: scripts/out)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
