"""Scheduler process entry point — cron_runs-guarded periodic enqueues."""

from __future__ import annotations

import asyncio

from app.core.config import Environment, get_settings
from app.core.logging import configure_logging
from app.infrastructure.telemetry.sentry import init_sentry
from app.jobs.scheduler import Scheduler


def main() -> None:
    settings = get_settings()
    configure_logging(json_output=settings.app_env is not Environment.DEVELOPMENT)
    init_sentry(settings)
    scheduler = Scheduler()
    asyncio.run(scheduler.run())


if __name__ == "__main__":
    main()
