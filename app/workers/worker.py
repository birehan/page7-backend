"""Worker process entry point — claims and runs jobs from every queue."""

from __future__ import annotations

import asyncio

from app.core.config import Environment, get_settings
from app.core.logging import configure_logging
from app.db import import_models as _import_models  # noqa: F401
from app.infrastructure.telemetry.sentry import init_sentry
from app.jobs.handlers import REGISTRY
from app.jobs.runner import Worker


def main() -> None:
    settings = get_settings()
    configure_logging(json_output=settings.app_env is not Environment.DEVELOPMENT)
    init_sentry(settings)
    worker = Worker(registry=REGISTRY)
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
