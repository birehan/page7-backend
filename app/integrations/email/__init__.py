from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.config import Environment, Settings, get_settings
from app.integrations.email.console import ConsoleEmailProvider
from app.integrations.email.ports import EmailProvider
from app.integrations.email.resend import ResendEmailProvider
from app.integrations.email.smtp import SmtpEmailProvider

_LOCAL_ENVS = (Environment.DEVELOPMENT, Environment.TESTING)


def get_email_provider(settings: Annotated[Settings, Depends(get_settings)]) -> EmailProvider:
    """architecture/06 §2: which adapter answers for a port is a configuration
    value, resolved once per call from settings, never a code change.
    """
    if settings.email.provider == "smtp":
        password = settings.email.smtp_password
        if password is None or not password.get_secret_value().strip():
            raise RuntimeError("EMAIL__SMTP_PASSWORD is not configured")
        return SmtpEmailProvider(
            host=settings.email.smtp_host,
            port=settings.email.smtp_port,
            username=settings.email.smtp_username,
            password=password.get_secret_value(),
            from_address=settings.email.from_address,
            use_tls=settings.email.smtp_use_tls,
        )

    resolve_to_console = settings.email.provider == "console" or (
        settings.email.provider == "auto" and settings.app_env in _LOCAL_ENVS
    )
    if resolve_to_console:
        return ConsoleEmailProvider()

    if settings.email.provider == "resend" or settings.email.provider == "auto":
        api_key = settings.email.resend_api_key
        if api_key is None:
            raise RuntimeError("EMAIL__RESEND_API_KEY is not configured")
        return ResendEmailProvider(
            api_key=api_key.get_secret_value(), from_address=settings.email.from_address
        )

    raise RuntimeError(f"Unknown EMAIL__PROVIDER: {settings.email.provider}")
