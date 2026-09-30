"""Parameterized 10×10 post status transition legality (service-layer)."""

from __future__ import annotations

import pytest

from app.features.posts.service import LEGAL_TRANSITIONS, POST_STATUSES, can_transition

# The 17 legal pairs from utils.ts / post_status_transitions seed.
LEGAL_PAIRS = frozenset(
    (frm, to) for frm, targets in LEGAL_TRANSITIONS.items() for to in targets
)


@pytest.mark.parametrize("frm", POST_STATUSES)
@pytest.mark.parametrize("to", POST_STATUSES)
def test_transition_legality_matches_seeded_table(frm: str, to: str) -> None:
    allowed = (frm, to) in LEGAL_PAIRS
    assert can_transition(frm, to) is allowed


def test_exactly_seventeen_legal_pairs() -> None:
    assert len(LEGAL_PAIRS) == 17
