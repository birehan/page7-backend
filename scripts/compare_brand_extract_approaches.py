#!/usr/bin/env python3
"""Compare fetch-only vs full (fetch+search) brand extraction across test sites.

Runs both approaches as separate jobs (in parallel), then writes a side-by-side
HTML report so you can judge which approach gets the job done.

Usage (from backend/):

    uv run python scripts/compare_brand_extract_approaches.py
    uv run python scripts/compare_brand_extract_approaches.py --concurrency 3
    uv run python scripts/compare_brand_extract_approaches.py --out scripts/out
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Allow `uv run python scripts/...` to import the sibling extract module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_onboarding_brand import extract_brand

# Diverse, currently fetchable Saudi targets (probed before shipping this script).
DEFAULT_SITES: tuple[tuple[str, str], ...] = (
    ("Obeid Hospitals", "https://obeidhospitals.com/"),
    ("Extra", "https://www.extra.com/"),
    ("Al Rajhi Bank", "https://www.alrajhibank.com.sa/"),
    ("Albaik", "https://www.albaik.com/"),
    ("Panda", "https://www.panda.sa/"),
)

APPROACHES: tuple[tuple[str, bool], ...] = (
    ("fetch_only", True),
    ("full", False),
)

_GUIDELINE_KEYS = (
    "voiceAdjectives",
    "doList",
    "dontList",
    "bannedClaims",
    "colors",
    "dialect",
    "languages",
)
_TOP_KEYS = ("pillars", "competitors", "logoUrl")


@dataclass(frozen=True)
class RunResult:
    site_name: str
    url: str
    approach: str
    status: str
    elapsed_seconds: float | None
    crawled_pages: int
    field_count: int
    fields_present: list[str]
    confidence: dict[str, float]
    warnings: list[str]
    patch: dict[str, Any]
    error: dict[str, Any] | None
    sources: list[str]


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _fields_present(patch: dict[str, Any]) -> list[str]:
    present: list[str] = []
    guidelines = patch.get("guidelines") or {}
    for key in _GUIDELINE_KEYS:
        if guidelines.get(key) not in (None, [], ""):
            present.append(key)
    for key in _TOP_KEYS:
        if patch.get(key) not in (None, [], ""):
            present.append(key)
    return present


async def _run_one(
    *,
    site_name: str,
    url: str,
    approach: str,
    fetch_only: bool,
    sem: asyncio.Semaphore,
) -> RunResult:
    async with sem:
        print(f"→ start {approach:11} {_host(url)}", flush=True)
        try:
            raw = await extract_brand(url, brand_name=site_name, fetch_only=fetch_only)
        except Exception as exc:  # noqa: BLE001 — report per-site failure in HTML
            print(f"✗ crash  {approach:11} {_host(url)}: {exc}", flush=True)
            return RunResult(
                site_name=site_name,
                url=url,
                approach=approach,
                status="failed",
                elapsed_seconds=None,
                crawled_pages=0,
                field_count=0,
                fields_present=[],
                confidence={},
                warnings=[f"exception: {exc}"],
                patch={},
                error={"code": "EXCEPTION", "message": str(exc)},
                sources=[],
            )

        patch = dict(raw.get("patch") or {})
        present = _fields_present(patch)
        status = str(raw.get("status") or "failed")
        warnings = list(raw.get("warnings") or [])
        if status == "failed":
            warnings = list(raw.get("fetch_warnings") or []) + list(
                raw.get("search_warnings") or []
            )
        elapsed = raw.get("elapsed_seconds")
        print(
            f"✓ done   {approach:11} {_host(url)} "
            f"status={status} fields={len(present)} "
            f"t={elapsed}s",
            flush=True,
        )
        return RunResult(
            site_name=site_name,
            url=url,
            approach=approach,
            status=status,
            elapsed_seconds=float(elapsed) if elapsed is not None else None,
            crawled_pages=int(raw.get("crawled_pages") or 0),
            field_count=len(present),
            fields_present=present,
            confidence={k: float(v) for k, v in (raw.get("confidence") or {}).items()},
            warnings=warnings,
            patch=patch,
            error=raw.get("error"),
            sources=list(raw.get("sources") or []),
        )


def _pick_winner(fetch_only: RunResult, full: RunResult) -> str:
    if fetch_only.status == "failed" and full.status == "failed":
        return "neither"
    if fetch_only.status == "failed":
        return "full"
    if full.status == "failed":
        return "fetch_only"

    # Prefer richer fields; if close (±1 field), prefer faster.
    if full.field_count >= fetch_only.field_count + 2:
        return "full"
    if fetch_only.field_count >= full.field_count + 2:
        return "fetch_only"

    fo_t = fetch_only.elapsed_seconds or 9999.0
    full_t = full.elapsed_seconds or 9999.0
    # Full must be clearly better on fields or we'd pick the faster one.
    if full.field_count > fetch_only.field_count and full_t <= fo_t * 1.35:
        return "full"
    if fo_t <= full_t:
        return "fetch_only"
    return "full"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}s"


def _status_class(status: str) -> str:
    return {
        "succeeded": "ok",
        "partial": "warn",
        "failed": "bad",
    }.get(status, "bad")


def _preview_value(value: Any, *, limit: int = 180) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        if not value:
            return "—"
        if isinstance(value[0], dict):
            text = ", ".join(
                str(item.get("name") or item.get("handle") or item) for item in value[:4]
            )
            if len(value) > 4:
                text += f" (+{len(value) - 4})"
        else:
            text = ", ".join(str(item) for item in value[:6])
            if len(value) > 6:
                text += f" (+{len(value) - 6})"
    else:
        text = str(value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _field_rows(fetch_only: RunResult, full: RunResult) -> str:
    rows: list[str] = []
    all_keys = list(_GUIDELINE_KEYS) + list(_TOP_KEYS)
    fo_g = (fetch_only.patch.get("guidelines") or {}) if fetch_only.patch else {}
    full_g = (full.patch.get("guidelines") or {}) if full.patch else {}

    for key in all_keys:
        if key in _GUIDELINE_KEYS:
            fo_val = fo_g.get(key)
            full_val = full_g.get(key)
        else:
            fo_val = fetch_only.patch.get(key)
            full_val = full.patch.get(key)

        fo_conf = fetch_only.confidence.get(key)
        full_conf = full.confidence.get(key)
        fo_filled = fo_val not in (None, [], "")
        full_filled = full_val not in (None, [], "")

        if fo_filled and full_filled:
            mark = "both"
        elif fo_filled:
            mark = "fetch_only"
        elif full_filled:
            mark = "full"
        else:
            mark = "neither"

        rows.append(
            "<tr>"
            f"<td><code>{_esc(key)}</code></td>"
            f"<td class='{_esc(mark)}'>{_esc(_preview_value(fo_val))}"
            f"<div class='conf'>"
            f"{_esc(f'{fo_conf:.2f}' if fo_conf is not None else '—')}</div></td>"
            f"<td class='{_esc(mark)}'>{_esc(_preview_value(full_val))}"
            f"<div class='conf'>"
            f"{_esc(f'{full_conf:.2f}' if full_conf is not None else '—')}</div></td>"
            f"<td class='tag {_esc(mark)}'>{_esc(mark)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _approach_card(run: RunResult) -> str:
    warns = "".join(f"<li>{_esc(w)}</li>" for w in run.warnings[:8]) or "<li>none</li>"
    err = ""
    if run.error:
        err = f"<p class='bad'><strong>Error:</strong> {_esc(run.error)}</p>"
    return f"""
    <div class="card">
      <h3>{_esc(run.approach)}</h3>
      <p><span class="pill {_esc(_status_class(run.status))}">{_esc(run.status)}</span>
         <span class="meta">{_esc(_fmt_seconds(run.elapsed_seconds))} ·
         {run.field_count} fields · {run.crawled_pages} pages</span></p>
      {err}
      <p><strong>Fields:</strong> {_esc(", ".join(run.fields_present) or "none")}</p>
      <p><strong>Sources:</strong> {_esc(", ".join(run.sources[:5]) or "none")}</p>
      <details>
        <summary>Warnings ({len(run.warnings)})</summary>
        <ul>{warns}</ul>
      </details>
      <details>
        <summary>Full patch JSON</summary>
        <pre>{_esc(json.dumps(run.patch, ensure_ascii=False, indent=2))}</pre>
      </details>
    </div>
    """


def _recommendation(results: list[RunResult]) -> tuple[str, str]:
    by_site: dict[str, dict[str, RunResult]] = {}
    for run in results:
        by_site.setdefault(run.site_name, {})[run.approach] = run

    votes = {"fetch_only": 0, "full": 0, "neither": 0}
    reasons: list[str] = []
    for site_name, pair in by_site.items():
        fo = pair["fetch_only"]
        full = pair["full"]
        winner = _pick_winner(fo, full)
        votes[winner] += 1
        reasons.append(
            f"{site_name}: {winner} "
            f"(fetch_only {fo.field_count} fields / {_fmt_seconds(fo.elapsed_seconds)}; "
            f"full {full.field_count} fields / {_fmt_seconds(full.elapsed_seconds)})"
        )

    if votes["fetch_only"] > votes["full"]:
        pick = "fetch_only"
        summary = (
            "Fetch-only wins on more sites: similar (or better) field coverage "
            "at much lower latency because it skips the 90s search stage."
        )
    elif votes["full"] > votes["fetch_only"]:
        pick = "full"
        summary = (
            "Full pipeline wins on more sites: search meaningfully filled gaps "
            "that fetch-only missed (worth the extra time)."
        )
    else:
        pick = "tie"
        summary = (
            "Split decision. Prefer fetch-only for onboarding speed, and run full "
            "only when fetch is thin or you specifically need competitors/third-party signals."
        )
    detail = " | ".join(reasons)
    return pick, f"{summary} Per-site: {detail}"


def render_html(results: list[RunResult], *, generated_at: str) -> str:
    by_site: dict[str, dict[str, RunResult]] = {}
    order: list[str] = []
    for run in results:
        if run.site_name not in by_site:
            order.append(run.site_name)
            by_site[run.site_name] = {}
        by_site[run.site_name][run.approach] = run

    pick, rec_text = _recommendation(results)

    summary_rows: list[str] = []
    for site_name in order:
        fo = by_site[site_name]["fetch_only"]
        full = by_site[site_name]["full"]
        winner = _pick_winner(fo, full)
        summary_rows.append(
            "<tr>"
            f"<td><strong>{_esc(site_name)}</strong><div class='muted'>{_esc(fo.url)}</div></td>"
            f"<td class='{_esc(_status_class(fo.status))}'>{_esc(fo.status)} · "
            f"{_esc(_fmt_seconds(fo.elapsed_seconds))} · {fo.field_count}f</td>"
            f"<td class='{_esc(_status_class(full.status))}'>{_esc(full.status)} · "
            f"{_esc(_fmt_seconds(full.elapsed_seconds))} · {full.field_count}f</td>"
            f"<td class='tag {_esc(winner)}'>{_esc(winner)}</td>"
            "</tr>"
        )

    sections: list[str] = []
    for site_name in order:
        fo = by_site[site_name]["fetch_only"]
        full = by_site[site_name]["full"]
        winner = _pick_winner(fo, full)
        sections.append(
            f"""
            <section class="site">
              <h2>{_esc(site_name)}
                <span class="tag {_esc(winner)}">pick: {_esc(winner)}</span>
              </h2>
              <p class="muted"><a href="{_esc(fo.url)}">{_esc(fo.url)}</a></p>
              <div class="grid">
                {_approach_card(fo)}
                {_approach_card(full)}
              </div>
              <h3>Field-by-field</h3>
              <table>
                <thead>
                  <tr><th>Field</th><th>fetch_only</th><th>full</th><th>Who filled</th></tr>
                </thead>
                <tbody>
                  {_field_rows(fo, full)}
                </tbody>
              </table>
            </section>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Brand extract approach comparison</title>
  <style>
    :root {{
      --bg: #f6f3ee;
      --ink: #1c1917;
      --muted: #78716c;
      --card: #fffdf9;
      --line: #e7e0d5;
      --ok: #166534;
      --warn: #92400e;
      --bad: #991b1b;
      --accent: #0f766e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 600px at 10% -10%, #dcebe7 0%, transparent 55%),
        radial-gradient(900px 500px at 100% 0%, #f0e2d0 0%, transparent 50%),
        var(--bg);
      line-height: 1.45;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 2rem 1.25rem 4rem; }}
    h1, h2, h3 {{ font-weight: 700; letter-spacing: -0.02em; }}
    h1 {{ font-size: clamp(1.8rem, 3vw, 2.4rem); margin: 0 0 0.4rem; }}
    h2 {{
      margin-top: 2.5rem;
      display: flex;
      gap: 0.75rem;
      align-items: baseline;
      flex-wrap: wrap;
    }}
    .lede {{ color: var(--muted); max-width: 60ch; margin-bottom: 1.5rem; }}
    .rec {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 1rem 1.2rem;
      margin: 1rem 0 1.5rem;
    }}
    .rec strong.pick {{ color: var(--accent); text-transform: uppercase; letter-spacing: 0.04em; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      font-size: 0.95rem;
    }}
    th, td {{
      text-align: left;
      vertical-align: top;
      padding: 0.7rem 0.8rem;
      border-bottom: 1px solid var(--line);
    }}
    th {{
      background: #f3eee6;
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
      margin: 1rem 0;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 1rem 1.1rem;
    }}
    .pill, .tag {{
      display: inline-block;
      border-radius: 999px;
      padding: 0.15rem 0.55rem;
      font-size: 0.78rem;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}
    .ok {{ color: var(--ok); }}
    .warn {{ color: var(--warn); }}
    .bad {{ color: var(--bad); }}
    .pill.ok, .tag.fetch_only {{ background: #dcfce7; color: var(--ok); }}
    .pill.warn {{ background: #ffedd5; color: var(--warn); }}
    .pill.bad, .tag.neither {{ background: #fee2e2; color: var(--bad); }}
    .tag.full {{ background: #ccfbf1; color: #115e59; }}
    .tag.both {{ background: #e7e5e4; color: #44403c; }}
    .tag.tie {{ background: #e7e5e4; color: #44403c; }}
    .meta, .muted, .conf {{ color: var(--muted); font-size: 0.9rem; }}
    .conf {{ margin-top: 0.25rem; font-family: ui-monospace, monospace; }}
    details {{ margin-top: 0.6rem; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #1c1917;
      color: #f5f5f4;
      padding: 0.8rem;
      border-radius: 10px;
      font-size: 0.78rem;
      max-height: 320px;
      overflow: auto;
    }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.86em; }}
    a {{ color: var(--accent); }}
    footer {{ margin-top: 3rem; color: var(--muted); font-size: 0.9rem; }}
  </style>
</head>
<body>
  <main>
    <h1>Brand extract: fetch-only vs full</h1>
    <p class="lede">
      Two separate approaches on five Saudi sites, run in parallel.
      <strong>fetch_only</strong> = website crawl + LLM extract.
      <strong>full</strong> = same, then web_search stage (90s cap) + merge.
      Generated {_esc(generated_at)}.
    </p>

    <div class="rec">
      <p><strong class="pick">Recommendation: {_esc(pick)}</strong></p>
      <p>{_esc(rec_text)}</p>
    </div>

    <h2>Scoreboard</h2>
    <table>
      <thead>
        <tr>
          <th>Site</th>
          <th>fetch_only</th>
          <th>full</th>
          <th>Local pick</th>
        </tr>
      </thead>
      <tbody>
        {"".join(summary_rows)}
      </tbody>
    </table>

    {"".join(sections)}

    <footer>
      Heuristic: prefer the approach that fills ≥2 more fields; otherwise prefer the faster one.
      Open this file in a browser after the script finishes.
    </footer>
  </main>
</body>
</html>
"""


async def run_comparison(
    sites: tuple[tuple[str, str], ...],
    *,
    concurrency: int,
) -> list[RunResult]:
    sem = asyncio.Semaphore(max(1, concurrency))
    tasks = [
        _run_one(
            site_name=name,
            url=url,
            approach=approach,
            fetch_only=fetch_only,
            sem=sem,
        )
        for name, url in sites
        for approach, fetch_only in APPROACHES
    ]
    return list(await asyncio.gather(*tasks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare fetch-only vs full brand extraction; write HTML report.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Max parallel extract jobs (default 4)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "out",
        help="Output directory for HTML + JSON",
    )
    args = parser.parse_args(argv)
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Comparing {len(DEFAULT_SITES)} sites × {len(APPROACHES)} approaches "
        f"(concurrency={args.concurrency})",
        flush=True,
    )
    results = asyncio.run(run_comparison(DEFAULT_SITES, concurrency=args.concurrency))

    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    html_path = out_dir / "brand_extract_comparison.html"
    json_path = out_dir / "brand_extract_comparison.json"
    html_path.write_text(render_html(results, generated_at=generated_at), encoding="utf-8")
    json_path.write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    pick, rec = _recommendation(results)
    print(f"\nWrote {html_path}", flush=True)
    print(f"Wrote {json_path}", flush=True)
    print(f"Recommendation: {pick}", flush=True)
    print(rec, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
