#!/usr/bin/env python3
"""Bake-off: new fetch-only vs pgblank-mapped on 10 sites (no field drops).

Both approaches keep the full field set — no --fast caps, no skipped LLM stages.

Usage (from backend/):

    uv run python scripts/bakeoff_10_sites_brand_extract.py
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_brand_via_pgblank import extract_via_pgblank
from extract_onboarding_brand import extract_brand

SITES: tuple[tuple[str, str], ...] = (
    ("Obeid Hospitals", "https://obeidhospitals.com/"),
    ("Extra", "https://www.extra.com/"),
    ("Albaik", "https://www.albaik.com/"),
    ("Al Rajhi Bank", "https://www.alrajhibank.com.sa/"),
    ("Panda", "https://www.panda.sa/"),
    ("HungerStation", "https://www.hungerstation.com/"),
    ("Careem", "https://www.careem.com/"),
    ("Saudia", "https://www.saudia.com/"),
    ("Mobily", "https://www.mobily.com.sa/"),
    ("Farm Superstores", "https://www.farm.com.sa/"),
)

# All onboarding patch fields + pgblank extras we care about keeping.
SCORE_FIELDS: tuple[str, ...] = (
    "voiceAdjectives",
    "doList",
    "dontList",
    "bannedClaims",
    "colors",
    "dialect",
    "languages",
    "pillars",
    "competitors",
    "logoUrl",
    "display_name",
    "tagline",
    "tone_description",
    "fonts",
    "topics",
    "offer_hypotheses",
)


@dataclass
class RunRow:
    site_name: str
    url: str
    approach: str
    status: str
    elapsed_seconds: float | None
    fields: dict[str, Any] = field(default_factory=dict)
    field_count: int = 0
    hex_color_count: int = 0
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _esc(v: object) -> str:
    return html.escape(str(v), quote=True)


def _filled(v: Any) -> bool:
    return v not in (None, "", [], {})


def _flatten_result(approach: str, site: str, url: str, raw: dict[str, Any]) -> RunRow:
    patch = dict(raw.get("patch") or {})
    g = dict(patch.get("guidelines") or {})
    extras = dict(raw.get("pgblank_extras") or {})
    fields = {
        "voiceAdjectives": g.get("voiceAdjectives"),
        "doList": g.get("doList"),
        "dontList": g.get("dontList"),
        "bannedClaims": g.get("bannedClaims"),
        "colors": g.get("colors"),
        "dialect": g.get("dialect"),
        "languages": g.get("languages"),
        "pillars": patch.get("pillars"),
        "competitors": patch.get("competitors"),
        "logoUrl": patch.get("logoUrl"),
        "display_name": extras.get("display_name"),
        "tagline": extras.get("tagline"),
        "tone_description": extras.get("tone_description"),
        "fonts": extras.get("fonts"),
        "topics": extras.get("topics"),
        "offer_hypotheses": extras.get("offer_hypotheses"),
    }
    colors = fields.get("colors") or []
    hex_n = sum(
        1 for c in colors if isinstance(c, str) and c.startswith("#") and len(c) >= 4
    )
    count = sum(1 for k in SCORE_FIELDS if _filled(fields.get(k)))
    return RunRow(
        site_name=site,
        url=url,
        approach=approach,
        status=str(raw.get("status") or "failed"),
        elapsed_seconds=(
            float(raw["elapsed_seconds"]) if raw.get("elapsed_seconds") is not None else None
        ),
        fields=fields,
        field_count=count,
        hex_color_count=hex_n,
        warnings=list(raw.get("warnings") or []),
        raw=raw,
    )


def _score(row: RunRow) -> tuple[int, int, float]:
    """Higher field_count, more hex colors, lower time."""
    status = {"succeeded": 2, "partial": 1, "failed": 0}.get(row.status, 0)
    t = row.elapsed_seconds if row.elapsed_seconds is not None else 9999.0
    return (status, row.field_count * 10 + row.hex_color_count, -t)


def _pick(a: RunRow, b: RunRow) -> str:
    if a.status == "failed" and b.status == "failed":
        return "neither"
    if a.status == "failed":
        return b.approach
    if b.status == "failed":
        return a.approach
    return a.approach if _score(a) >= _score(b) else b.approach


async def _run_one(
    *,
    site: str,
    url: str,
    approach: str,
    sem: asyncio.Semaphore,
) -> RunRow:
    async with sem:
        print(f"→ {approach:22} {urlparse(url).netloc}", flush=True)
        t0 = time.perf_counter()
        try:
            if approach == "new_fetch_only":
                raw = await extract_brand(url, brand_name=site, fetch_only=True)
            else:
                raw = await extract_via_pgblank(url, brand_name=site)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {approach:22} {urlparse(url).netloc}: {exc}", flush=True)
            return RunRow(
                site_name=site,
                url=url,
                approach=approach,
                status="failed",
                elapsed_seconds=round(time.perf_counter() - t0, 3),
                warnings=[f"exception: {exc}"],
            )
        row = _flatten_result(approach, site, url, raw)
        print(
            f"✓ {approach:22} {urlparse(url).netloc} "
            f"status={row.status} fields={row.field_count} "
            f"colors={row.hex_color_count} t={row.elapsed_seconds}s",
            flush=True,
        )
        return row


def render_html(rows: list[RunRow], *, generated_at: str) -> str:
    by: dict[str, dict[str, RunRow]] = {}
    order: list[str] = []
    for r in rows:
        if r.site_name not in by:
            order.append(r.site_name)
            by[r.site_name] = {}
        by[r.site_name][r.approach] = r

    votes = {"new_fetch_only": 0, "pgblank_default_mapped": 0, "neither": 0}
    score_rows = []
    for site in order:
        a = by[site]["new_fetch_only"]
        b = by[site]["pgblank_default_mapped"]
        winner = _pick(a, b)
        votes[winner] = votes.get(winner, 0) + 1
        score_rows.append(
            "<tr>"
            f"<td><strong>{_esc(site)}</strong><div class='muted'>{_esc(a.url)}</div></td>"
            f"<td>{_esc(a.status)} · {a.field_count}f · {a.hex_color_count} hex · "
            f"{_esc(a.elapsed_seconds)}s</td>"
            f"<td>{_esc(b.status)} · {b.field_count}f · {b.hex_color_count} hex · "
            f"{_esc(b.elapsed_seconds)}s</td>"
            f"<td class='tag {_esc(winner)}'>{_esc(winner)}</td>"
            "</tr>"
        )

    if votes["pgblank_default_mapped"] > votes["new_fetch_only"]:
        overall = "pgblank_default_mapped"
        blurb = (
            "pgblank-mapped wins more sites on field coverage + measured colors "
            "without dropping guideline fields (do/dont/banned filled via new schema)."
        )
    elif votes["new_fetch_only"] > votes["pgblank_default_mapped"]:
        overall = "new_fetch_only"
        blurb = (
            "new fetch-only wins more sites (speed and/or guideline fill). "
            "pgblank still stronger on multi-color / fonts / display_name when it succeeds."
        )
    else:
        overall = "tie"
        blurb = (
            "Split. Use pgblank-mapped when you need colors/fonts/identity extras; "
            "fetch-only when latency matters and theme-color is enough."
        )

    sections = []
    for site in order:
        a = by[site]["new_fetch_only"]
        b = by[site]["pgblank_default_mapped"]
        winner = _pick(a, b)
        cells = []
        for key in SCORE_FIELDS:
            av, bv = a.fields.get(key), b.fields.get(key)
            if _filled(av) and _filled(bv):
                mark = "both"
            elif _filled(av):
                mark = "new_only"
            elif _filled(bv):
                mark = "pgblank_only"
            else:
                mark = "neither"
            cells.append(
                "<tr>"
                f"<td><code>{_esc(key)}</code></td>"
                f"<td class='{_esc(mark)}'>{_esc(_preview(av))}</td>"
                f"<td class='{_esc(mark)}'>{_esc(_preview(bv))}</td>"
                f"<td class='tag {_esc(mark)}'>{_esc(mark)}</td>"
                "</tr>"
            )
        sections.append(
            f"<section><h2>{_esc(site)} "
            f"<span class='tag {_esc(winner)}'>pick: {_esc(winner)}</span></h2>"
            f"<table><thead><tr><th>Field</th><th>new_fetch_only</th>"
            f"<th>pgblank_mapped</th><th>Who</th></tr></thead>"
            f"<tbody>{''.join(cells)}</tbody></table></section>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>10-site brand extract bake-off</title>
<style>
body{{margin:0;font-family:Georgia,serif;background:#f4f1ea;color:#1c1917;
  line-height:1.45}}
main{{max-width:1100px;margin:0 auto;padding:2rem 1.2rem 4rem}}
.rec,.card{{background:#fffdf8;border:1px solid #e7e0d5;border-radius:14px;
  padding:1rem;margin:1rem 0}}
table{{width:100%;border-collapse:collapse;background:#fffdf8;
  border:1px solid #e7e0d5}}
th,td{{padding:.6rem .7rem;border-bottom:1px solid #e7e0d5;vertical-align:top;
  text-align:left;font-size:.92rem}}
th{{background:#f3eee6;font-size:.78rem;text-transform:uppercase}}
.tag{{display:inline-block;border-radius:999px;padding:.1rem .45rem;
  font:600 .72rem system-ui,sans-serif;text-transform:uppercase}}
.tag.pgblank_default_mapped,.tag.pgblank_only{{background:#ccfbf1}}
.tag.new_fetch_only,.tag.new_only{{background:#dcfce7}}
.tag.both{{background:#e7e5e4}}.tag.neither,.tag.tie{{background:#fee2e2}}
.muted{{color:#78716c;font-size:.9rem}}
code{{font-family:ui-monospace,monospace;font-size:.86em}}
h2{{margin-top:2.2rem}}
</style></head><body><main>
<h1>10-site bake-off (no field drops)</h1>
<p class="muted">new fetch-only vs pgblank default mapped (Firecrawl+dembrandt+both LLMs).
Generated {_esc(generated_at)}.</p>
<div class="rec"><p><strong>Overall: {_esc(overall)}</strong></p>
<p>Votes — new: {votes['new_fetch_only']}, pgblank: {votes['pgblank_default_mapped']},
neither: {votes['neither']}.</p>
<p>{_esc(blurb)}</p></div>
<h2>Scoreboard</h2>
<table><thead><tr><th>Site</th><th>new_fetch_only</th><th>pgblank_mapped</th><th>Pick</th></tr></thead>
<tbody>{''.join(score_rows)}</tbody></table>
{''.join(sections)}
</main></body></html>
"""


def _preview(value: Any, *, limit: int = 140) -> str:
    if not _filled(value):
        return "—"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            text = ", ".join(
                str(i.get("name") or i.get("handle") or i) for i in value[:3]
            )
        else:
            text = ", ".join(str(x) for x in value[:5])
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def run_all(*, concurrency: int) -> list[RunRow]:
    sem = asyncio.Semaphore(max(1, concurrency))
    tasks = []
    for name, url in SITES:
        tasks.append(_run_one(site=name, url=url, approach="new_fetch_only", sem=sem))
        tasks.append(
            _run_one(site=name, url=url, approach="pgblank_default_mapped", sem=sem)
        )
    return list(await asyncio.gather(*tasks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "out",
    )
    args = parser.parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    print(
        f"10-site bake-off × 2 full approaches (concurrency={args.concurrency})",
        flush=True,
    )
    rows = asyncio.run(run_all(concurrency=args.concurrency))
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    html_path = out / "bakeoff_10_sites.html"
    json_path = out / "bakeoff_10_sites.json"
    html_path.write_text(render_html(rows, generated_at=generated), encoding="utf-8")
    json_path.write_text(
        json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Tally
    by: dict[str, dict[str, RunRow]] = {}
    for r in rows:
        by.setdefault(r.site_name, {})[r.approach] = r
    votes = {"new_fetch_only": 0, "pgblank_default_mapped": 0, "neither": 0}
    for site, pair in by.items():
        w = _pick(pair["new_fetch_only"], pair["pgblank_default_mapped"])
        votes[w] += 1
        print(
            f"  {site}: {w} "
            f"(new={pair['new_fetch_only'].field_count}f/"
            f"{pair['new_fetch_only'].elapsed_seconds}s, "
            f"pg={pair['pgblank_default_mapped'].field_count}f/"
            f"{pair['pgblank_default_mapped'].elapsed_seconds}s)",
            flush=True,
        )
    overall = max(votes, key=lambda k: votes[k])
    print(f"\nWrote {html_path}", flush=True)
    print(f"Wrote {json_path}", flush=True)
    print(f"Overall winner: {overall} {votes}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
