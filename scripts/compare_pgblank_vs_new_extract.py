#!/usr/bin/env python3
"""Compare /home/babi/new fetch-only extract vs /home/babi/pgblank measured+LLM.

Runs both approaches in parallel on the same Saudi sites and writes an HTML
report with a field-gap matrix.

Usage (from backend/):

    uv run python scripts/compare_pgblank_vs_new_extract.py
    uv run python scripts/compare_pgblank_vs_new_extract.py --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_onboarding_brand import extract_brand

PGBLANK_API = Path("/home/babi/pgblank/apps/api")
DEFAULT_SITES: tuple[tuple[str, str], ...] = (
    ("Obeid Hospitals", "https://obeidhospitals.com/"),
    ("Extra", "https://www.extra.com/"),
    ("Albaik", "https://www.albaik.com/"),
)

# Fields we care about for the gap matrix (union of both systems).
MATRIX_FIELDS: tuple[str, ...] = (
    "display_name",
    "tagline",
    "tone_description",
    "voice",
    "do_list",
    "dont_list",
    "banned_claims",
    "colors",
    "fonts",
    "color_scheme",
    "dialect",
    "languages",
    "logo_url",
    "pillars",
    "competitors",
    "topics",
    "offer_hypotheses",
    "social_links",
    "contact_links",
)


@dataclass
class SideResult:
    system: str
    site_name: str
    url: str
    status: str
    elapsed_seconds: float | None
    fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _preview(value: Any, *, limit: int = 160) -> str:
    if value in (None, "", [], {}):
        return "—"
    if isinstance(value, list):
        if not value:
            return "—"
        if isinstance(value[0], dict):
            text = ", ".join(
                str(
                    item.get("name")
                    or item.get("name_en")
                    or item.get("handle")
                    or item
                )
                for item in value[:4]
            )
            if len(value) > 4:
                text += f" (+{len(value) - 4})"
        else:
            text = ", ".join(str(x) for x in value[:6])
            if len(value) > 6:
                text += f" (+{len(value) - 6})"
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _filled(value: Any) -> bool:
    return value not in (None, "", [], {})


def _load_new_openai_key() -> str | None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return None
    text = env_path.read_text(encoding="utf-8")
    for pattern in (
        r"^LLM__OPENAI_API_KEY=(.+)$",
        r"^OPENAI_API_KEY=(.+)$",
    ):
        match = re.search(pattern, text, flags=re.MULTILINE)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return None


def _normalize_new(site_name: str, url: str, raw: dict[str, Any]) -> SideResult:
    patch = dict(raw.get("patch") or {})
    guidelines = dict(patch.get("guidelines") or {})
    fields = {
        "display_name": None,
        "tagline": None,
        "tone_description": None,
        "voice": guidelines.get("voiceAdjectives"),
        "do_list": guidelines.get("doList"),
        "dont_list": guidelines.get("dontList"),
        "banned_claims": guidelines.get("bannedClaims"),
        "colors": guidelines.get("colors"),
        "fonts": None,
        "color_scheme": None,
        "dialect": guidelines.get("dialect"),
        "languages": guidelines.get("languages"),
        "logo_url": patch.get("logoUrl"),
        "pillars": patch.get("pillars"),
        "competitors": patch.get("competitors"),
        "topics": None,
        "offer_hypotheses": None,
        "social_links": None,
        "contact_links": None,
    }
    return SideResult(
        system="new_fetch_only",
        site_name=site_name,
        url=url,
        status=str(raw.get("status") or "failed"),
        elapsed_seconds=(
            float(raw["elapsed_seconds"]) if raw.get("elapsed_seconds") is not None else None
        ),
        fields=fields,
        raw=raw,
        warnings=list(raw.get("warnings") or []),
        error=raw.get("error"),
    )


def _normalize_pgblank(site_name: str, url: str, raw: dict[str, Any]) -> SideResult:
    draft = dict(raw.get("draft") or {})
    measured = dict(raw.get("measured") or {})
    colors = draft.get("color_palette") or measured.get("colors")
    fields = {
        "display_name": draft.get("display_name") or None,
        "tagline": draft.get("tagline") or None,
        "tone_description": draft.get("tone_description") or None,
        "voice": draft.get("voice_traits") or None,
        "do_list": None,
        "dont_list": None,
        "banned_claims": None,
        "colors": colors,
        "fonts": measured.get("fonts") or None,
        "color_scheme": measured.get("color_scheme") or None,
        "dialect": None,
        "languages": None,
        "logo_url": draft.get("logo_url") or measured.get("logo_url"),
        "pillars": draft.get("pillars") or None,
        "competitors": None,
        "topics": draft.get("topics") or None,
        "offer_hypotheses": draft.get("offer_hypotheses") or None,
        "social_links": None,
        "contact_links": None,
    }
    return SideResult(
        system="pgblank_measured_llm",
        site_name=site_name,
        url=url,
        status=str(raw.get("status") or "failed"),
        elapsed_seconds=(
            float(raw["timing"]["elapsed_seconds"])
            if isinstance(raw.get("timing"), dict)
            and raw["timing"].get("elapsed_seconds") is not None
            else None
        ),
        fields=fields,
        raw=raw,
        warnings=list(raw.get("warnings") or []),
        error=raw.get("error"),
    )


async def _run_new(
    site_name: str, url: str, sem: asyncio.Semaphore
) -> SideResult:
    async with sem:
        print(f"→ new      {_host(url)}", flush=True)
        try:
            raw = await extract_brand(url, brand_name=site_name, fetch_only=True)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ new      {_host(url)}: {exc}", flush=True)
            return SideResult(
                system="new_fetch_only",
                site_name=site_name,
                url=url,
                status="failed",
                elapsed_seconds=None,
                warnings=[f"exception: {exc}"],
                error={"code": "EXCEPTION", "message": str(exc)},
            )
        side = _normalize_new(site_name, url, raw)
        print(
            f"✓ new      {_host(url)} status={side.status} "
            f"t={side.elapsed_seconds}s",
            flush=True,
        )
        return side


async def _run_pgblank(
    site_name: str,
    url: str,
    sem: asyncio.Semaphore,
    *,
    openai_key: str | None,
) -> SideResult:
    async with sem:
        print(f"→ pgblank  {_host(url)}", flush=True)
        env = os.environ.copy()
        env["FIRECRAWL_BASE_URL"] = "http://127.0.0.1:3002"
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key
        proc = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "python",
            "scripts/export_brand_extract_json.py",
            url,
            cwd=str(PGBLANK_API),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        text = stdout.decode("utf-8", errors="replace").strip()
        # JSON may be preceded by logs if any leaked; take last {...} block.
        raw: dict[str, Any]
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}\s*$", text)
            if not match:
                err = stderr.decode("utf-8", errors="replace")[-500:]
                print(f"✗ pgblank  {_host(url)}: bad JSON", flush=True)
                return SideResult(
                    system="pgblank_measured_llm",
                    site_name=site_name,
                    url=url,
                    status="failed",
                    elapsed_seconds=None,
                    warnings=[f"bad JSON stdout; stderr={err}"],
                    error={"code": "BAD_JSON", "message": err or text[-300:]},
                )
            raw = json.loads(match.group(0))
        side = _normalize_pgblank(site_name, url, raw)
        print(
            f"✓ pgblank  {_host(url)} status={side.status} "
            f"t={side.elapsed_seconds}s",
            flush=True,
        )
        return side


def _field_mark(new_val: Any, pg_val: Any) -> str:
    n, p = _filled(new_val), _filled(pg_val)
    if n and p:
        return "both"
    if n:
        return "new_only"
    if p:
        return "pgblank_only"
    return "neither"


def _missing_for_new(results: list[SideResult]) -> list[str]:
    """Fields pgblank filled at least once that new never filled."""
    by_site: dict[str, dict[str, SideResult]] = {}
    for r in results:
        by_site.setdefault(r.site_name, {})[r.system] = r

    missing: list[str] = []
    for key in MATRIX_FIELDS:
        pg_ever = False
        new_ever = False
        for pair in by_site.values():
            new_s = pair.get("new_fetch_only")
            pg_s = pair.get("pgblank_measured_llm")
            if new_s and _filled(new_s.fields.get(key)):
                new_ever = True
            if pg_s and _filled(pg_s.fields.get(key)):
                pg_ever = True
        if pg_ever and not new_ever:
            missing.append(key)
    return missing


def _recommendation(results: list[SideResult]) -> tuple[str, str]:
    by_site: dict[str, dict[str, SideResult]] = {}
    for r in results:
        by_site.setdefault(r.site_name, {})[r.system] = r

    notes: list[str] = []
    guidelines_new = 0
    visuals_pg = 0
    identity_pg = 0
    for site, pair in by_site.items():
        new_s = pair["new_fetch_only"]
        pg_s = pair["pgblank_measured_llm"]
        new_guide = sum(
            1
            for k in ("voice", "do_list", "dont_list", "banned_claims", "languages")
            if _filled(new_s.fields.get(k))
        )
        pg_guide = sum(
            1
            for k in ("voice", "tone_description")
            if _filled(pg_s.fields.get(k))
        )
        if new_guide >= pg_guide:
            guidelines_new += 1
        pg_colors = _filled(pg_s.fields.get("colors")) and len(pg_s.fields.get("colors") or []) > 1
        new_colors = _filled(new_s.fields.get("colors"))
        if pg_colors and (not new_colors or len(new_s.fields.get("colors") or []) <= 1):
            visuals_pg += 1
        if _filled(pg_s.fields.get("fonts")):
            visuals_pg += 0  # counted via colors primarily
        if _filled(pg_s.fields.get("display_name")) or _filled(pg_s.fields.get("tagline")):
            identity_pg += 1
        notes.append(
            f"{site}: new_guide={new_guide} pg_guide={pg_guide} "
            f"new_t={new_s.elapsed_seconds} pg_t={pg_s.elapsed_seconds}"
        )

    pick = (
        "split: new for guidelines / pgblank for visuals+identity"
        if guidelines_new and (visuals_pg or identity_pg)
        else ("new_fetch_only" if guidelines_new >= len(by_site) // 2 else "pgblank_measured_llm")
    )
    summary = (
        f"Guidelines lean new on {guidelines_new}/{len(by_site)} sites; "
        f"identity fields from pgblank on {identity_pg}/{len(by_site)}; "
        f"richer measured colors from pgblank on {visuals_pg}/{len(by_site)}. "
        + " | ".join(notes)
    )
    return pick, summary


def render_html(results: list[SideResult], *, generated_at: str) -> str:
    by_site: dict[str, dict[str, SideResult]] = {}
    order: list[str] = []
    for r in results:
        if r.site_name not in by_site:
            order.append(r.site_name)
            by_site[r.site_name] = {}
        by_site[r.site_name][r.system] = r

    pick, rec = _recommendation(results)
    missing = _missing_for_new(results)

    # Also list static scrape-but-not-proposal gaps for new.
    static_gaps = [
        "display_name / site title (scraped as title, not in proposal)",
        "tagline / OG description",
        "fonts (needs measured tooling like dembrandt)",
        "multi-color palette beyond theme-color",
        "topics / offer_hypotheses",
        "social_links / contact_links as first-class proposal fields",
        "bilingual pillar names (name_en / name_ar)",
    ]

    score_rows = []
    for site in order:
        new_s = by_site[site]["new_fetch_only"]
        pg_s = by_site[site]["pgblank_measured_llm"]
        new_n = sum(1 for k in MATRIX_FIELDS if _filled(new_s.fields.get(k)))
        pg_n = sum(1 for k in MATRIX_FIELDS if _filled(pg_s.fields.get(k)))
        score_rows.append(
            "<tr>"
            f"<td><strong>{_esc(site)}</strong><div class='muted'>{_esc(new_s.url)}</div></td>"
            f"<td>{_esc(new_s.status)} · {_esc(new_s.elapsed_seconds)}s · {new_n}f</td>"
            f"<td>{_esc(pg_s.status)} · {_esc(pg_s.elapsed_seconds)}s · {pg_n}f</td>"
            "</tr>"
        )

    sections = []
    for site in order:
        new_s = by_site[site]["new_fetch_only"]
        pg_s = by_site[site]["pgblank_measured_llm"]
        rows = []
        for key in MATRIX_FIELDS:
            nv, pv = new_s.fields.get(key), pg_s.fields.get(key)
            mark = _field_mark(nv, pv)
            rows.append(
                "<tr>"
                f"<td><code>{_esc(key)}</code></td>"
                f"<td class='{_esc(mark)}'>{_esc(_preview(nv))}</td>"
                f"<td class='{_esc(mark)}'>{_esc(_preview(pv))}</td>"
                f"<td class='tag {_esc(mark)}'>{_esc(mark)}</td>"
                "</tr>"
            )
        sections.append(
            f"""
            <section class="site">
              <h2>{_esc(site)}</h2>
              <p class="muted"><a href="{_esc(new_s.url)}">{_esc(new_s.url)}</a></p>
              <div class="grid">
                <div class="card">
                  <h3>new fetch-only</h3>
                  <p>{_esc(new_s.status)} · {_esc(new_s.elapsed_seconds)}s</p>
                  <details><summary>Raw JSON</summary>
                    <pre>{_esc(json.dumps(new_s.raw, ensure_ascii=False, indent=2))}</pre>
                  </details>
                </div>
                <div class="card">
                  <h3>pgblank measured+LLM</h3>
                  <p>{_esc(pg_s.status)} · {_esc(pg_s.elapsed_seconds)}s</p>
                  <details><summary>Raw JSON</summary>
                    <pre>{_esc(json.dumps(pg_s.raw, ensure_ascii=False, indent=2))}</pre>
                  </details>
                </div>
              </div>
              <h3>Field matrix</h3>
              <table>
                <thead><tr><th>Field</th><th>new</th><th>pgblank</th><th>Who</th></tr></thead>
                <tbody>{"".join(rows)}</tbody>
              </table>
            </section>
            """
        )

    missing_li = (
        "".join(f"<li><code>{_esc(m)}</code></li>" for m in missing)
        or "<li>none empirically</li>"
    )
    static_li = "".join(f"<li>{_esc(s)}</li>" for s in static_gaps)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>pgblank vs new brand extraction</title>
  <style>
    :root {{
      --bg:#f4f1ea; --ink:#1c1917; --muted:#78716c; --card:#fffdf8;
      --line:#e7e0d5; --ok:#166534; --warn:#92400e; --teal:#0f766e;
    }}
    body {{
      margin:0; color:var(--ink);
      font-family: "Iowan Old Style", Palatino, Georgia, serif;
      background:
        radial-gradient(900px 500px at 0% 0%, #dcebe7, transparent 55%),
        radial-gradient(800px 400px at 100% 0%, #f0e2d0, transparent 50%),
        var(--bg);
      line-height:1.45;
    }}
    main {{ max-width:1100px; margin:0 auto; padding:2rem 1.2rem 4rem; }}
    h1 {{ margin:0 0 .4rem; letter-spacing:-.02em; }}
    .lede,.muted {{ color:var(--muted); }}
    .rec,.card {{
      background:var(--card); border:1px solid var(--line);
      border-radius:14px; padding:1rem 1.1rem; margin:1rem 0;
    }}
    .rec strong {{ color:var(--teal); text-transform:uppercase; letter-spacing:.04em; }}
    table {{ width:100%; border-collapse:collapse; background:var(--card);
      border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
    th,td {{ text-align:left; vertical-align:top; padding:.65rem .75rem;
      border-bottom:1px solid var(--line); font-size:.95rem; }}
    th {{ background:#f3eee6; font-size:.8rem; text-transform:uppercase; letter-spacing:.04em; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:1rem; }}
    .tag {{ display:inline-block; border-radius:999px; padding:.12rem .5rem;
      font:600 .75rem ui-sans-serif,system-ui,sans-serif; text-transform:uppercase; }}
    .tag.both,.both {{ background:#e7e5e4; }}
    .tag.new_only,.new_only {{ background:#dcfce7; color:var(--ok); }}
    .tag.pgblank_only,.pgblank_only {{ background:#ccfbf1; color:#115e59; }}
    .tag.neither,.neither {{ background:#fee2e2; color:#991b1b; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:#1c1917; color:#f5f5f4;
      padding:.8rem; border-radius:10px; font-size:.75rem; max-height:280px; overflow:auto; }}
    code {{ font-family:ui-monospace,Menlo,monospace; font-size:.86em; }}
    a {{ color:var(--teal); }}
    h2 {{ margin-top:2.4rem; }}
  </style>
</head>
<body>
<main>
  <h1>pgblank vs new brand extraction</h1>
  <p class="lede">
    <strong>new</strong> = httpx HTML scrape + gpt-5.5 (fetch-only).
    <strong>pgblank</strong> = Firecrawl markdown + dembrandt visuals + OpenAI brand card.
    Generated {_esc(generated_at)}.
  </p>
  <div class="rec">
    <p><strong>Recommendation: {_esc(pick)}</strong></p>
    <p>{_esc(rec)}</p>
  </div>

  <h2>Scoreboard</h2>
  <table>
    <thead><tr><th>Site</th><th>new fetch-only</th><th>pgblank</th></tr></thead>
    <tbody>{"".join(score_rows)}</tbody>
  </table>

  <h2>Fields we are not extracting now (that we can)</h2>
  <div class="card">
    <p><strong>Empirically filled by pgblank, never by new on these sites:</strong></p>
    <ul>{missing_li}</ul>
    <p><strong>Also available / scrapable in new today but not in the proposal schema:</strong></p>
    <ul>{static_li}</ul>
  </div>

  {"".join(sections)}
</main>
</body>
</html>
"""


async def run_all(*, concurrency: int) -> list[SideResult]:
    sem = asyncio.Semaphore(max(1, concurrency))
    openai_key = _load_new_openai_key()
    tasks = []
    for name, url in DEFAULT_SITES:
        tasks.append(_run_new(name, url, sem))
        tasks.append(_run_pgblank(name, url, sem, openai_key=openai_key))
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
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Comparing {len(DEFAULT_SITES)} sites × 2 systems "
        f"(concurrency={args.concurrency})",
        flush=True,
    )
    results = asyncio.run(run_all(concurrency=args.concurrency))
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    html_path = out_dir / "pgblank_vs_new_comparison.html"
    json_path = out_dir / "pgblank_vs_new_comparison.json"
    html_path.write_text(render_html(results, generated_at=generated_at), encoding="utf-8")
    json_path.write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pick, rec = _recommendation(results)
    missing = _missing_for_new(results)
    print(f"\nWrote {html_path}", flush=True)
    print(f"Wrote {json_path}", flush=True)
    print(f"Recommendation: {pick}", flush=True)
    print(rec, flush=True)
    print(f"Missing for new: {', '.join(missing) or '(none)'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
