"""Application metrics home (architecture/13 §4).

No Prometheus exporter is wired yet (Phase 15) — instruments are recorded on
the OTel meter so they are ready when the scrape endpoint lands.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

from opentelemetry import metrics

from app.integrations.errors import ProviderError

meter = metrics.get_meter("page7.api")

_provider_call_duration = meter.create_histogram(
    name="provider_call_duration_seconds",
    description="Duration of outbound provider calls",
    unit="s",
)

_provider_call_errors = meter.create_counter(
    name="provider_call_errors_total",
    description="Outbound provider call errors by type",
)


@asynccontextmanager
async def provider_call(
    *,
    provider: str,
    operation: str,
) -> AsyncIterator[None]:
    """Record duration (and error class on failure) for one provider call."""
    started = perf_counter()
    attributes = {"provider": provider, "operation": operation}
    try:
        yield
    except ProviderError as exc:
        _provider_call_errors.add(
            1,
            {**attributes, "error": type(exc).__name__},
        )
        raise
    except Exception as exc:
        _provider_call_errors.add(
            1,
            {**attributes, "error": type(exc).__name__},
        )
        raise
    finally:
        _provider_call_duration.record(perf_counter() - started, attributes)
