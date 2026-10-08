from __future__ import annotations

from html import escape

from app.core.config import get_settings
from app.integrations.email.ports import EmailMessage

_CTA_STYLE = (
    "display:inline-block;background:#2563eb;color:#ffffff;"
    "padding:12px 20px;text-decoration:none;border-radius:6px;font-weight:600;"
)

_INVITE_CONTENT = {
    "ar": {
        "subject": "دعوة للانضمام إلى {org} على Page7",
        "html": (
            '<div dir="rtl" lang="ar">'
            "<p>مرحباً،</p>"
            "<p>دعاك {inviter} للانضمام إلى {org} على Page7.</p>"
            f'<p><a href="{{link}}" style="{_CTA_STYLE}">قبول الدعوة</a></p>'
            "</div>"
        ),
        "text": "دعاك {inviter} للانضمام إلى {org} على Page7: {link}",
    },
    "en": {
        "subject": "You're invited to join {org} on Page7",
        "html": (
            '<div dir="ltr" lang="en">'
            "<p>Hi,</p>"
            "<p>{inviter} invited you to join {org} on Page7.</p>"
            f'<p><a href="{{link}}" style="{_CTA_STYLE}">Accept invitation</a></p>'
            "</div>"
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

_VERIFY_EMAIL_CONTENT = {
    "ar": {
        "subject": "رمز التحقق — Page7",
        "html": (
            "<p>مرحباً،</p>"
            "<p>رمز التحقق الخاص بك هو:</p>"
            '<p style="font-size:24px;font-weight:bold;letter-spacing:4px;">{code}</p>'
            "<p>ينتهي هذا الرمز خلال ١٠ دقائق. إذا لم تطلب ذلك، تجاهل هذه الرسالة.</p>"
        ),
        "text": "رمز التحقق الخاص بك على Page7: {code} (صالح لمدة ١٠ دقائق)",
    },
    "en": {
        "subject": "Your verification code — Page7",
        "html": (
            "<p>Hi,</p>"
            "<p>Your Page7 verification code is:</p>"
            '<p style="font-size:24px;font-weight:bold;letter-spacing:4px;">{code}</p>'
            "<p>This code expires in 10 minutes. If you didn't request it, ignore this email.</p>"
        ),
        "text": "Your Page7 verification code: {code} (expires in 10 minutes)",
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
    locale_norm = locale if locale in _INVITE_CONTENT else "ar"
    content = _INVITE_CONTENT[locale_norm]
    link = f"{get_settings().auth.frontend_url}/{locale_norm}/invite/{token}"
    safe_inviter = escape(inviter_name)
    safe_org = escape(organization_name)
    return EmailMessage(
        to=to,
        subject=content["subject"].format(org=organization_name),
        html=content["html"].format(inviter=safe_inviter, org=safe_org, link=link),
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


def verify_email_otp_email(
    *, to: str, code: str, locale: str, idempotency_key: str
) -> EmailMessage:
    content = _VERIFY_EMAIL_CONTENT.get(locale, _VERIFY_EMAIL_CONTENT["en"])
    return EmailMessage(
        to=to,
        subject=content["subject"],
        html=content["html"].format(code=code),
        text=content["text"].format(code=code),
        idempotency_key=idempotency_key,
    )
