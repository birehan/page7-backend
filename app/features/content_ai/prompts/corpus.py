"""Load the caption corpus fixture pinned to copywriter-v5."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "caption_corpus.json"


@lru_cache(maxsize=1)
def load_caption_corpus() -> list[dict[str, object]]:
    """Return the 24-theme caption corpus as a list of dicts."""
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        data: object = json.load(fh)
    if not isinstance(data, list):
        raise TypeError("caption_corpus.json must be a JSON array")
    return [entry for entry in data if isinstance(entry, dict)]
