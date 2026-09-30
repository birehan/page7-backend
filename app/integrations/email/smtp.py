"""SMTP email adapter (Hostinger mailbox, etc.).

Callers: app.integrations.email.get_email_provider when EMAIL__PROVIDER=smtp.
API: EmailProvider.send → SMTP AUTH + send.
User instruction: setup SMTP and try it (Hostinger contact@page7.io).
"""

from __future__ import annotations

from email.message import EmailMessage as StdlibEmailMessage

import aiosmtplib

from app.integrations.email.ports import EmailMessage
from app.integrations.errors import ProviderAuthError, ProviderUnavailableError


class SmtpEmailProvider:
    """Sends via SMTP (e.g. Hostinger smtp.hostinger.com)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        use_tls: bool,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._use_tls = use_tls

    async def send(self, message: EmailMessage) -> None:
        mime = StdlibEmailMessage()
        mime["From"] = self._from_address
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime["Message-ID"] = f"<{message.idempotency_key}@page7.io>"
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")

        try:
            # Port 465: implicit TLS. Port 587: plain then STARTTLS.
            if self._use_tls:
                await aiosmtplib.send(
                    mime,
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    use_tls=True,
                )
            else:
                await aiosmtplib.send(
                    mime,
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    start_tls=True,
                )
        except aiosmtplib.SMTPAuthenticationError as exc:
            raise ProviderAuthError(str(exc)) from exc
        except aiosmtplib.SMTPException as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        except OSError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
