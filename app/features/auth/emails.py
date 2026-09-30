from __future__ import annotations

from app.core.config import get_settings
from app.integrations.email.ports import EmailMessage

_INVITE_CONTENT = {
    "ar": {
        "subject": "دعوة للانضمام إلى {org} على Page7",
        "html": (
            "<p>مرحباً،</p>"
            "<p>دعاك {inviter} للانضمام إلى {org} على Page7.</p>"
            '<p><a href="{link}">قبول الدعوة</a></p>'
        ),
        "text": "دعاك {inviter} للانضمام إلى {org} على Page7: {link}",
    },
    "en": {
        "subject": "You're invited to join {org} on Page7",
        "html": (
            "<p>Hi,</p>"
            "<p>{inviter} invited you to join {org} on Page7.</p>"
            '<p><a href="{link}">Accept invite</a></p>'
        ),
        "text": "{inviter} invited you to join {org} on Page7: {link}",
    },
}

_RESET_CONTENT = {
    "ar": {
        "subject": "إعادة تعيين كلمة المرور — Page7",
        "html": (
            "<p>مرحباً،</p>"
            "<p>اضغط على الرابط لإعادة تعيين كلمة المرور:</p>"
            '<p><a href="{link}">إعادة تعيين كلمة المرور</a></p>'
            "<p>إذا لم تطلب ذلك، تجاهل هذه الرسالة.</p>"
        ),
        "text": "لإعادة تعيين كلمة المرور: {link}",
    },
    "en": {
        "subject": "Reset your password — Page7",
        "html": (
            "<p>Hi,</p>"
            "<p>Click the link below to reset your password:</p>"
            '<p><a href="{link}">Reset password</a></p>'
            "<p>If you didn't request this, ignore this email.</p>"
        ),
        "text": "Reset your password: {link}",
    },
}


def invite_email(
    *,
    to: str,
    inviter_name: str,
    organization_name: str,
    token: str,
    locale: str,
    idempotency_key: str,
) -> EmailMessage:
    content = _INVITE_CONTENT.get(locale, _INVITE_CONTENT["ar"])
    link = f"{get_settings().auth.frontend_url}/{locale}/invite/{token}"
    return EmailMessage(
        to=to,
        subject=content["subject"].format(org=organization_name),
        html=content["html"].format(inviter=inviter_name, org=organization_name, link=link),
        text=content["text"].format(inviter=inviter_name, org=organization_name, link=link),
        idempotency_key=idempotency_key,
    )


def password_reset_email(*, to: str, token: str, locale: str, idempotency_key: str) -> EmailMessage:
    content = _RESET_CONTENT.get(locale, _RESET_CONTENT["ar"])
    link = f"{get_settings().auth.frontend_url}/{locale}/reset-password/{token}"
    return EmailMessage(
        to=to,
        subject=content["subject"],
        html=content["html"].format(link=link),
        text=content["text"].format(link=link),
        idempotency_key=idempotency_key,
    )
