from __future__ import annotations

from decimal import Decimal

# Per-million-token input/output rates in USD, sourced from docs/reference/provider-facts.md.
# gpt-5.5 verified 2026-09-04 against OpenAI pricing pages ($5/$30 short-context tier).
_MODEL_RATES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-6-astra": (Decimal("10"), Decimal("50")),
    "gpt-5.6-luna": (Decimal("0.20"), Decimal("1.20")),
    "gpt-5.5": (Decimal("5.00"), Decimal("30.00")),
    "claude-fable-5-1": (Decimal("10"), Decimal("50")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5-20251001": (Decimal("1"), Decimal("5")),
}

_SEARCH_COST_PER_CALL = Decimal("10") / Decimal("1000")
_MTOK = Decimal("1000000")


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    search_calls: int = 0,
) -> Decimal:
    """Estimate USD cost from token counts and optional web-search surcharges."""
    rates = _MODEL_RATES.get(model)
    if rates is None:
        return Decimal("0")
    input_rate, output_rate = rates
    token_cost = (
        Decimal(prompt_tokens) * input_rate + Decimal(completion_tokens) * output_rate
    ) / _MTOK
    search_cost = Decimal(search_calls) * _SEARCH_COST_PER_CALL
    return token_cost + search_cost
