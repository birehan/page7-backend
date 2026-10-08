from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider

from app.core.config import Settings


def init_tracing(app: FastAPI, settings: Settings) -> None:
    """Off by default (`OBSERVABILITY__OTEL_ENABLED=false`) until a real exporter
    is configured — architecture/13 §5 pins the SDK and FastAPI instrumentation
    versions together deliberately; bump them as a pair, never independently.
    """
    if not settings.observability.otel_enabled:
        return
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "page7-api"}))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
