"""Phase 2 Definition of Done: the capability-matrix parity test passes
byte-for-byte against docs/contracts/capabilities.json.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.request_context import CAPABILITY_MATRIX

CONTRACT_PATH = Path(__file__).parents[3] / "docs" / "contracts" / "capabilities.json"


def test_capability_matrix_matches_frontend_fixture() -> None:
    frontend = json.loads(CONTRACT_PATH.read_text())
    backend = {role: sorted(caps) for role, caps in CAPABILITY_MATRIX.items()}
    assert backend == frontend
