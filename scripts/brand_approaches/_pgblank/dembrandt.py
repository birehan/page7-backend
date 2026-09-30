"""dembrandt CLI adapter — measured colors / logo / fonts via Playwright.

Vendored for lab use under brand_approaches (no pgblank apps/api dependency).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from .branding import MeasuredBranding, normalize_dembrandt

log = logging.getLogger(__name__)

PROVIDER = "dembrandt"
_DEFAULT_TIMEOUT_SECONDS = 90.0
_PGBLANK_DEMBRANDT = Path(
    "/home/babi/pgblank/tools/brand-extract-compare/node_modules/dembrandt/dist/index.js"
)


def resolve_dembrandt_bin(explicit: str = "") -> str | None:
    """Locate dembrandt entry or executable. None when unavailable."""
    if explicit.strip():
        path = Path(explicit.strip())
        return str(path) if path.is_file() or shutil.which(explicit.strip()) else None

    which = shutil.which("dembrandt")
    if which:
        return which

    candidates = [
        _PGBLANK_DEMBRANDT,
        Path("/usr/local/lib/node_modules/dembrandt/dist/index.js"),
        Path("/usr/lib/node_modules/dembrandt/dist/index.js"),
        Path.cwd() / "node_modules" / "dembrandt" / "dist" / "index.js",
        Path("/app/node_modules/dembrandt/dist/index.js"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _parse_json_payload(stdout: str) -> object:
    trimmed = stdout.strip()
    if not trimmed:
        return None
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        start = trimmed.find("{")
        end = trimmed.rfind("}")
        if start >= 0 and end > start:
            return json.loads(trimmed[start : end + 1])
        raise


class DembrandtExtractor:
    """Runs dembrandt CLI and normalizes stdout JSON to MeasuredBranding."""

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._bin = resolve_dembrandt_bin(bin_path or os.environ.get("DEMBRANDT_BIN", ""))
        self._timeout = timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self._bin)

    async def extract(self, url: str) -> MeasuredBranding:
        if not self._bin:
            log.warning("dembrandt unavailable (binary not found)")
            return MeasuredBranding()

        cmd, args = self._command(url)
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
        except OSError as exc:
            log.warning("dembrandt spawn failed: %s", exc)
            return MeasuredBranding()

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("dembrandt timeout url=%s timeout_s=%s", url, self._timeout)
            return MeasuredBranding()

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        code = proc.returncode or 0

        if code != 0 and not stdout.strip():
            log.warning("dembrandt failed url=%s code=%s stderr=%s", url, code, stderr[:500])
            return MeasuredBranding()

        try:
            raw = _parse_json_payload(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("dembrandt invalid json url=%s error=%s", url, exc)
            return MeasuredBranding()

        return normalize_dembrandt(raw if isinstance(raw, dict) else {})

    def _command(self, url: str) -> tuple[str, list[str]]:
        bin_path = self._bin
        if bin_path is None:
            raise RuntimeError("dembrandt binary missing")
        if bin_path.endswith(".js"):
            return "node", [bin_path, url, "--json-only"]
        return bin_path, [url, "--json-only"]
