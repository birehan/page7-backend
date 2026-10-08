#!/usr/bin/env python3
"""Fal imagegen bakeoff — compare pipeline variants and emit an HTML gallery.

Callers: manual CLI (`python scripts/imagegen_bakeoff/run_bakeoff.py`).
Requires IMAGEGEN__API_KEY (or FAL_KEY) for live runs; `--dry-run` skips Fal.

Variants:
  A  caption → Flux Flash (legacy baseline)
  B  rewrite → Flux Flash
  C  rewrite → Flux 2 Pro
  D  rewrite → GPT Image 1.5 medium
  E  poster EN Ideogram vs AR Qwen
  F  best photo + logo composite

User: implement bakeoff-harness from production AI image gen plan.

Usage:
  cd page7-backend && python scripts/imagegen_bakeoff/run_bakeoff.py
  python scripts/imagegen_bakeoff/run_bakeoff.py --dry-run --limit 2
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.features.visuals.aspects import size_for_aspect  # noqa: E402
from app.features.visuals.composite import apply_brand_finish  # noqa: E402
from app.features.visuals.prompt_rewrite import (  # noqa: E402
    assemble_generation_prompt,
    color_names_from_hex,
    heuristic_brief,
)
from app.integrations.imagegen.cost import estimate_image_cost_usd  # noqa: E402
from app.integrations.imagegen.ports import (  # noqa: E402
    ImageGenerationRequest,
    ParamProfile,
)

# FalImageProvider imported lazily in _main_async when not --dry-run.

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures.json"
OUT_DIR = HERE / "out"

def _make_placeholder_logo() -> bytes:
    """Simple teal badge PNG for logo-composite variants when --logo-path omitted."""
    from io import BytesIO

    from PIL import Image as PILImage
    from PIL import ImageDraw

    img = PILImage.new("RGBA", (256, 96), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, 255, 95), radius=16, fill=(14, 116, 144, 255))
    draw.rectangle((24, 28, 232, 68), fill=(255, 255, 255, 230))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

_NO_TEXT = "no text, no letters, no typography, no watermarks, no captions"


@dataclass
class VariantResult:
    fixture_id: str
    variant: str
    model_id: str
    prompt: str
    latency_ms: int
    cost_usd: str
    width: int
    height: int
    image_path: str | None
    error: str | None
    notes: str


def _load_fixtures(limit: int | None) -> list[dict[str, Any]]:
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data[:limit] if limit else data


def _style_suffix(style: str) -> str:
    if style == "poster":
        return (
            "clean social media poster layout, legible typography, "
            "Saudi-market appropriate (no alcohol, no pork, modest dress)"
        )
    mapping = {
        "photo": f"professional photography, natural lighting, {_NO_TEXT}",
        "flat": f"flat vector illustration, no gradients, {_NO_TEXT}",
        "three-d": f"3D render, soft studio lighting, {_NO_TEXT}",
        "minimal": f"minimalist, generous negative space, {_NO_TEXT}",
        "saudi-modern": f"modern Saudi aesthetic, geometric pattern motifs, {_NO_TEXT}",
    }
    return mapping.get(style, mapping["photo"])


def _variants_for(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    style = fixture["style"]
    if style == "poster":
        if fixture.get("language") == "ar":
            return [
                {
                    "id": "E-qwen",
                    "rewrite": True,
                    "model_id": "alibaba/qwen-image-3/text-to-image",
                    "profile": "qwen",
                    "composite_logo": True,
                    "notes": "poster AR → Qwen + logo composite",
                },
                {
                    "id": "E-gpt",
                    "rewrite": True,
                    "model_id": "fal-ai/gpt-image-1.5",
                    "profile": "gpt_image",
                    "composite_logo": True,
                    "notes": "poster AR → GPT Image + logo composite",
                },
            ]
        return [
            {
                "id": "E-ideogram",
                "rewrite": True,
                "model_id": "fal-ai/ideogram/v3",
                "profile": "ideogram",
                "composite_logo": True,
                "notes": "poster EN → Ideogram + logo composite",
            },
            {
                "id": "E-qwen",
                "rewrite": True,
                "model_id": "alibaba/qwen-image-3/text-to-image",
                "profile": "qwen",
                "composite_logo": True,
                "notes": "poster EN → Qwen control",
            },
        ]

    return [
        {
            "id": "A",
            "rewrite": False,
            "model_id": "fal-ai/flux-2/flash",
            "profile": "flux",
            "composite_logo": False,
            "notes": "legacy: raw caption → Flash",
        },
        {
            "id": "B",
            "rewrite": True,
            "model_id": "fal-ai/flux-2/flash",
            "profile": "flux",
            "composite_logo": False,
            "notes": "rewrite → Flash",
        },
        {
            "id": "C",
            "rewrite": True,
            "model_id": "fal-ai/flux-2-pro",
            "profile": "flux",
            "composite_logo": False,
            "notes": "rewrite → Flux 2 Pro (production default)",
        },
        {
            "id": "D",
            "rewrite": True,
            "model_id": "fal-ai/gpt-image-1.5",
            "profile": "gpt_image",
            "composite_logo": False,
            "notes": "rewrite → GPT Image medium",
        },
        {
            "id": "F",
            "rewrite": True,
            "model_id": "fal-ai/flux-2-pro",
            "profile": "flux",
            "composite_logo": True,
            "notes": "rewrite → Pro + logo composite",
        },
    ]


def _build_prompt(fixture: dict[str, Any], *, rewrite: bool) -> tuple[str, str | None]:
    style = fixture["style"]
    mode = "poster" if style == "poster" else "photo"
    colors = fixture.get("colors") or []
    names = color_names_from_hex(colors)
    if rewrite:
        brief = heuristic_brief(
            caption_or_prompt=fixture["caption"],
            style=style,
            industry=fixture.get("industry"),
            city=fixture.get("city"),
            color_names=names,
            mode=mode,
        )
        prompt = assemble_generation_prompt(
            brief,
            style_prefix="",
            style_suffix=_style_suffix(style),
            industry=fixture.get("industry"),
            city=fixture.get("city"),
            headline=fixture.get("headline"),
            brand_colors_named=names,
            use_brand_colors=True,
            saudi_policy=mode == "poster",
            mode=mode,
        )
        return prompt, brief.negative_prompt or None

    parts = [fixture["caption"], _style_suffix(style)]
    if fixture.get("industry"):
        parts.append(f"brand industry: {fixture['industry']}")
    if colors:
        parts.append("brand palette: " + ", ".join(colors))
    return ". ".join(parts), None


async def _run_one(
    provider: Any,
    fixture: dict[str, Any],
    variant: dict[str, Any],
    *,
    dry_run: bool,
    logo_bytes: bytes,
    out_dir: Path,
) -> VariantResult:
    prompt, negative = _build_prompt(fixture, rewrite=bool(variant["rewrite"]))
    image_size = size_for_aspect(fixture["aspect"])
    model_id = variant["model_id"]
    profile: ParamProfile = variant["profile"]

    if dry_run or provider is None:
        return VariantResult(
            fixture_id=fixture["id"],
            variant=variant["id"],
            model_id=model_id,
            prompt=prompt,
            latency_ms=0,
            cost_usd="0",
            width=image_size["width"],
            height=image_size["height"],
            image_path=None,
            error=None,
            notes=f"dry-run; {variant['notes']}",
        )

    request = ImageGenerationRequest(
        prompt=prompt,
        style=fixture["style"],  # type: ignore[arg-type]
        aspect=fixture["aspect"],  # type: ignore[arg-type]
        brand_color_hint=fixture.get("colors") or [],
        model_id=model_id,
        image_size=image_size,
        param_profile=profile,
        enable_safety_checker=profile == "flux",
        negative_prompt=negative,
        quality_tier="standard",
    )
    started = time.perf_counter()
    try:
        result = await provider.generate_one(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        import httpx

        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
            resp = await client.get(result.image.url)
            resp.raise_for_status()
            raw = resp.content

        tw, th = image_size["width"], image_size["height"]
        # Some providers return JPEG/WebP; normalize via Pillow before finish.
        from io import BytesIO

        from PIL import Image as PILImage

        try:
            with PILImage.open(BytesIO(raw)) as img:
                buf = BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                raw = buf.getvalue()
        except Exception as decode_exc:
            raise RuntimeError(
                f"downloaded bytes not an image ({len(raw)}B, "
                f"content-type={resp.headers.get('content-type')}): {decode_exc}"
            ) from decode_exc

        if variant.get("composite_logo"):
            finished = apply_brand_finish(
                raw, target_width=tw, target_height=th, logo_bytes=logo_bytes
            )
        else:
            finished = apply_brand_finish(
                raw, target_width=tw, target_height=th, logo_bytes=None
            )

        rel = f"{fixture['id']}__{variant['id']}.png"
        path = out_dir / "images" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(finished)
        cost = estimate_image_cost_usd(model_id, tw, th)
        return VariantResult(
            fixture_id=fixture["id"],
            variant=variant["id"],
            model_id=model_id,
            prompt=prompt,
            latency_ms=latency_ms,
            cost_usd=str(cost),
            width=tw,
            height=th,
            image_path=str(path.relative_to(out_dir)),
            error=None,
            notes=variant["notes"],
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - started) * 1000)
        return VariantResult(
            fixture_id=fixture["id"],
            variant=variant["id"],
            model_id=model_id,
            prompt=prompt,
            latency_ms=latency_ms,
            cost_usd="0",
            width=image_size["width"],
            height=image_size["height"],
            image_path=None,
            error=str(exc),
            notes=variant["notes"],
        )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _write_gallery(results: list[VariantResult], out_dir: Path) -> Path:
    rows = []
    for r in results:
        img = (
            f'<img src="{r.image_path}" alt="{r.fixture_id} {r.variant}" />'
            if r.image_path
            else f'<p class="err">{r.error or "no image"}</p>'
        )
        rows.append(
            f"""
            <figure>
              <figcaption>
                <strong>{r.fixture_id}</strong> · {r.variant}<br/>
                <code>{r.model_id}</code><br/>
                {r.latency_ms}ms · ${r.cost_usd} · {r.width}×{r.height}<br/>
                <em>{r.notes}</em>
              </figcaption>
              {img}
              <details><summary>prompt</summary><pre>{_esc(r.prompt)}</pre></details>
            </figure>
            """
        )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Page7 imagegen bakeoff</title>
<style>
body{{font-family:system-ui,sans-serif;margin:1.5rem;background:#111;color:#eee}}
h1{{font-size:1.25rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem}}
figure{{margin:0;background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:.75rem}}
img{{width:100%;height:auto;border-radius:4px;display:block}}
figcaption{{font-size:.75rem;margin-bottom:.5rem;line-height:1.4}}
pre{{white-space:pre-wrap;font-size:.65rem;max-height:8rem;overflow:auto}}
.err{{color:#f88}}
code{{font-size:.7rem}}
</style></head><body>
<h1>Page7 imagegen bakeoff · {datetime.now(timezone.utc).isoformat()}</h1>
<p>Rubric (score 1–5 offline): brand-fit, platform crop, no garbled text,
Saudi-appropriate, logo fidelity, usable without edit.</p>
<div class="grid">{"".join(rows)}</div>
</body></html>"""
    path = out_dir / "gallery.html"
    path.write_text(html, encoding="utf-8")
    return path


def _write_csv(results: list[VariantResult], out_dir: Path) -> Path:
    path = out_dir / "scores.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "fixture_id",
                "variant",
                "model_id",
                "latency_ms",
                "cost_usd",
                "width",
                "height",
                "image_path",
                "error",
                "notes",
                "score_brand_fit",
                "score_crop",
                "score_text",
                "score_saudi",
                "score_logo",
                "score_usable",
            ],
        )
        writer.writeheader()
        for r in results:
            row = asdict(r)
            row.pop("prompt", None)
            for key in (
                "score_brand_fit",
                "score_crop",
                "score_text",
                "score_saudi",
                "score_logo",
                "score_usable",
            ):
                row[key] = ""
            writer.writerow(row)
    return path


async def _main_async(args: argparse.Namespace) -> int:
    fixtures = _load_fixtures(args.limit)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = OUT_DIR / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    logo_bytes = (
        Path(args.logo_path).read_bytes() if args.logo_path else _make_placeholder_logo()
    )

    api_key = os.environ.get("IMAGEGEN__API_KEY") or os.environ.get("FAL_KEY") or ""
    dry_run = args.dry_run or not api_key
    provider: Any = None
    if not dry_run:
        from app.integrations.imagegen.fal import FalImageProvider

        os.environ.setdefault("FAL_KEY", api_key)
        provider = FalImageProvider(api_key=api_key, timeout_seconds=180.0)

    results: list[VariantResult] = []
    for fixture in fixtures:
        for variant in _variants_for(fixture):
            if args.variants and variant["id"] not in args.variants:
                continue
            print(
                f"→ {fixture['id']} / {variant['id']} ({variant['model_id']})",
                file=sys.stderr,
                flush=True,
            )
            result = await _run_one(
                provider,
                fixture,
                variant,
                dry_run=dry_run,
                logo_bytes=logo_bytes,
                out_dir=out_dir,
            )
            results.append(result)
            (out_dir / "results.jsonl").open("a", encoding="utf-8").write(
                json.dumps(asdict(result), ensure_ascii=False) + "\n"
            )

    gallery = _write_gallery(results, out_dir)
    csv_path = _write_csv(results, out_dir)
    summary = {
        "dry_run": dry_run,
        "count": len(results),
        "gallery": str(gallery),
        "csv": str(csv_path),
        "errors": sum(1 for r in results if r.error),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["errors"] == 0 or dry_run else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Page7 Fal imagegen bakeoff")
    parser.add_argument("--dry-run", action="store_true", help="Skip Fal calls")
    parser.add_argument("--limit", type=int, default=None, help="Max fixtures")
    parser.add_argument(
        "--variants",
        nargs="*",
        default=None,
        help="Only these variant ids (A B C D E-ideogram E-qwen E-gpt F)",
    )
    parser.add_argument("--logo-path", default=None, help="PNG logo for variant F")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
