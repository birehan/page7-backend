from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_VALUES = {"", "changeme", "change-me", "placeholder", "replace_me"}


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://pgblank:pgblank@localhost:5544/pgblank"
    pool_size: int = 10
    echo: bool = False


class AuthSettings(BaseModel):
    session_cookie_name: str = "pgblank_session"
    # The invite-accept and reset-password links in transactional emails
    # point here — matches CORS's own dev default, deliberately a separate
    # setting rather than reusing cors.allowed_origins[0], since "which
    # origins may call the API" and "where the frontend actually lives" are
    # different concerns that happen to coincide only in this single-origin
    # setup.
    frontend_url: str = "http://localhost:3000"
    # "lax" is the default and the Cloudflare same-origin path. "none" is only
    # for the Firebase Hosting + cross-site Cloud Run API fallback (Phase 15).
    cookie_samesite: Literal["lax", "none"] = "lax"
    # Google OIDC — all three must be set for Google sign-in to be enabled.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None

    def google_configured(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret is not None
            and self.google_redirect_uri
        )


class CORSSettings(BaseModel):
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])


class StorageSettings(BaseModel):
    """architecture/11 — R2-backed ObjectStorage. `provider="auto"` resolves to
    LocalStorage under development/testing, R2 otherwise. `private_bucket` is
    optional until Phase 13 (invoices); resolving the private bucket while it
    is unset raises at the call site rather than at startup.
    """

    provider: Literal["auto", "r2", "local"] = "auto"
    public_bucket: str = "pgblank"
    public_base_url: str = "http://localhost:8000"
    private_bucket: str | None = None
    local_root: str = "/tmp/pgblank-storage"  # noqa: S108
    r2_account_id: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _empty_public_base_url_is_unset(cls, value: object) -> object:
        # `STORAGE__PUBLIC_BASE_URL=` in .env is present-but-empty and would
        # otherwise override the default, yielding relative LocalStorage URLs
        # that the web client rejects (z.string().url()).
        if isinstance(value, str) and not value.strip():
            return "http://localhost:8000"
        return value


class StockSettings(BaseModel):
    """architecture/06 §6 — Unsplash decided in Phase 5. `provider="auto"`
    resolves to the fake under development/testing, Unsplash otherwise.
    """

    provider: Literal["auto", "unsplash", "fake"] = "auto"
    unsplash_access_key: SecretStr | None = None


class TaskConfig(BaseModel):
    provider: Literal["openai", "anthropic"]
    model: str
    timeout_seconds: float = 30.0
    max_retries: int = 2
    temperature: float = 0.7
    max_output_tokens: int | None = None
    requires_tools: list[Literal["web_search", "web_fetch"]] = Field(default_factory=list)
    fallback_provider: Literal["openai", "anthropic"] | None = None
    fallback_model: str | None = None


TOOL_CAPABLE_MODELS: dict[str, set[str]] = {
    "openai": {"gpt-6-astra", "gpt-5.5"},
    "anthropic": {"claude-sonnet-5", "claude-opus-5", "claude-fable-5-1"},
}


def _default_tasks() -> dict[str, TaskConfig]:
    # OpenAI-only for all text LLM tasks. Image generation stays on IMAGEGEN__
    # (Fal) and is unrelated to these task configs.
    creative = TaskConfig(
        provider="openai",
        model="gpt-5.5",
        timeout_seconds=25,
        temperature=0.9,
    )
    light = TaskConfig(
        provider="openai",
        model="gpt-5.6-luna",
        timeout_seconds=25,
        temperature=0.9,
    )
    classification = TaskConfig(
        provider="openai",
        model="gpt-5.6-luna",
        timeout_seconds=10,
        temperature=0.0,
    )
    return {
        "caption_generation": creative,
        "caption_transform": light,
        # Full 14-day bilingual calendars routinely exceed the creative default (25s).
        "plan_generation": creative.model_copy(
            update={
                "temperature": 0.7,
                "timeout_seconds": 90,
                "max_output_tokens": 16384,
            }
        ),
        "strategy_generation": creative.model_copy(update={"temperature": 0.7}),
        "alt_text": light,
        "brand_research": TaskConfig(
            provider="openai",
            model="gpt-5.5",
            timeout_seconds=45,
            max_retries=1,
            temperature=0.2,
            # web_search is requested explicitly by SearchAugmentedResearcher only.
            # FetchExtractResearcher shares this task and must not inherit tools —
            # injecting web_search there burns budget and can stall extraction.
        ),
        "insight_report": TaskConfig(
            provider="openai",
            model="gpt-5.5",
            timeout_seconds=30,
            temperature=0.7,
        ),
        "classification": classification,
        "reply_suggestion": classification,
        # Caption → Flux-safe visual brief (architecture/09 v2).
        "visual_brief": light.model_copy(
            update={"temperature": 0.4, "timeout_seconds": 20}
        ),
    }


class LLMSettings(BaseModel):
    """architecture/07 — OpenAI/Anthropic-backed content AI."""

    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    provider: Literal["auto", "openai", "anthropic", "fake"] = "auto"
    rate_limit_captions: int = 60
    rate_limit_plan: int = 20
    rate_limit_strategy: int = 10
    rate_limit_alt_text: int = 30
    rate_limit_feedback: int = 120
    rate_limit_window_seconds: int = 60
    tasks: dict[str, TaskConfig] = Field(default_factory=_default_tasks)

    @model_validator(mode="after")
    def _validate_tool_capable_models(self) -> LLMSettings:
        for name, cfg in self.tasks.items():
            for tool in cfg.requires_tools:
                capable = TOOL_CAPABLE_MODELS.get(cfg.provider, set())
                if cfg.model not in capable:
                    raise ValueError(
                        f"task {name}: model {cfg.model} cannot use {tool}"
                    )
                if cfg.fallback_provider and cfg.fallback_model:
                    fb_capable = TOOL_CAPABLE_MODELS.get(cfg.fallback_provider, set())
                    if tool == "web_search" and cfg.fallback_model not in fb_capable:
                        raise ValueError(
                            f"task {name}: fallback model {cfg.fallback_model} "
                            f"cannot use {tool}"
                        )
        return self


class StyleConfig(BaseModel):
    """Per-style fal model and prompt augmentation (architecture/09 §2 / v2).

    Callers: model_resolve.resolve_model, visuals.service.start_generate.
    User: make Qwen Image 3 default for image generation here and onboarding.
    """

    model_id: str = "alibaba/qwen-image-3/text-to-image"
    draft_model_id: str | None = "fal-ai/flux-2/flash"
    premium_model_id: str | None = "fal-ai/ideogram/v3"
    param_profile: Literal["flux", "qwen", "ideogram", "gpt_image"] = "qwen"
    draft_param_profile: Literal["flux", "qwen", "ideogram", "gpt_image"] | None = "flux"
    premium_param_profile: Literal["flux", "qwen", "ideogram", "gpt_image"] | None = (
        "ideogram"
    )
    # Poster EN routing (AR stays on model_id / qwen).
    en_model_id: str | None = None
    en_param_profile: Literal["flux", "qwen", "ideogram", "gpt_image"] | None = None
    prompt_prefix: str = ""
    prompt_suffix: str = ""
    num_inference_steps: int | None = None
    guidance_scale: float | None = None


_NO_TEXT = (
    "no text, no letters, no typography, no watermarks, no captions"
)


def _default_styles() -> dict[str, StyleConfig]:
    """Style → model mapping (bakeoff-locked defaults).

    Standard photo styles use Qwen Image 3 (best bilingual / Saudi-market pick
    vs Ideogram). Draft stays on cheap Flux Flash; Premium uses Ideogram.
    Poster: Qwen for AR, Ideogram for EN headlines.
    """
    qwen_photo = StyleConfig(
        model_id="alibaba/qwen-image-3/text-to-image",
        draft_model_id="fal-ai/flux-2/flash",
        premium_model_id="fal-ai/ideogram/v3",
        param_profile="qwen",
        draft_param_profile="flux",
        premium_param_profile="ideogram",
    )
    return {
        "photo": qwen_photo.model_copy(
            update={
                "prompt_suffix": f"professional photography, natural lighting, {_NO_TEXT}",
            }
        ),
        "flat": qwen_photo.model_copy(
            update={
                "prompt_suffix": f"flat vector illustration, no gradients, {_NO_TEXT}",
            }
        ),
        "three-d": qwen_photo.model_copy(
            update={
                "prompt_suffix": f"3D render, soft studio lighting, {_NO_TEXT}",
            }
        ),
        "minimal": qwen_photo.model_copy(
            update={
                "prompt_suffix": f"minimalist, generous negative space, {_NO_TEXT}",
            }
        ),
        "saudi-modern": qwen_photo.model_copy(
            update={
                "prompt_suffix": (
                    f"modern Saudi aesthetic, geometric pattern motifs, {_NO_TEXT}"
                ),
            }
        ),
        "poster": StyleConfig(
            model_id="alibaba/qwen-image-3/text-to-image",
            draft_model_id="alibaba/qwen-image-3/text-to-image",
            premium_model_id="fal-ai/ideogram/v3",
            param_profile="qwen",
            draft_param_profile="qwen",
            premium_param_profile="ideogram",
            en_model_id="fal-ai/ideogram/v3",
            en_param_profile="ideogram",
            prompt_suffix=(
                "clean social media poster layout, legible typography, "
                "Saudi-market appropriate (no alcohol, no pork, modest dress)"
            ),
            num_inference_steps=None,
        ),
    }


def _default_aspect_map() -> dict[str, dict[str, int]]:
    from app.features.visuals.aspects import default_aspect_map

    return default_aspect_map()


class ImageGenSettings(BaseModel):
    """Fal image generation (architecture/09). Env prefix IMAGEGEN__."""

    api_key: SecretStr | None = None
    provider: Literal["auto", "fal", "fake"] = "auto"
    styles: dict[str, StyleConfig] = Field(default_factory=_default_styles)
    aspect_map: dict[str, dict[str, int]] = Field(default_factory=_default_aspect_map)
    rate_limit_generate: int = 20
    rate_limit_window_seconds: int = 60
    timeout_seconds: float = 120.0
    provider_url_ttl_days: int = 7


class ResearchSettings(BaseModel):
    """Website research / brand extraction (architecture/08). All defaulted —
    no new required environment variables beyond Phase 7's LLM keys.

    approach:
      - fast (default): homepage HTML signals + structured LLM (no search)
      - full: FetchExtractResearcher + SearchAugmentedResearcher + merge
    Override with RESEARCH__APPROACH=full to roll back.
    """

    daily_cap_per_brand: int = 10
    page_cap: int = 5
    approach: Literal["fast", "full"] = "fast"


class SocialSettings(BaseModel):
    """Zernio social accounts (architecture/10, Phase 9).

    API keys are *not* nested under SOCIAL__ — they live at the process env
    as `ZERNIO_API_KEY__{alias}`, matching `zernio_credentials.secret_ref`.
    Resolve at call time via `integrations.social.resolve_zernio_secret`.
    Optional override: `ZERNIO_CREDENTIAL_ALIASES=t1,t2,t3,t4` (comma-separated)
    is read by the factory when present; otherwise `credential_aliases` below.
    """

    provider: Literal["auto", "zernio", "fake"] = "auto"
    credential_aliases: list[str] = Field(
        default_factory=lambda: ["t1", "t2", "t3", "t4"]
    )
    max_profiles: int = 100
    max_accounts_per_credential: int = 2
    fill_to_tier: int = 3
    callback_redirect_origin: str = "http://localhost:3000"
    base_url: str = "https://zernio.com/api/v1"
    oauth_callback_url: str = "http://localhost:8000/v1/integrations/zernio/callback"


class EmailSettings(BaseModel):
    """architecture/06 §3's configuration-driven adapter factory: `"auto"`
    resolves to `console` under `development`/`testing`, `resend` otherwise.
    Explicit `smtp` uses Hostinger (or any) SMTP even in local envs.
    """

    resend_api_key: SecretStr | None = None
    from_address: str = "Page7 <contact@page7.io>"
    provider: Literal["auto", "resend", "console", "smtp"] = "auto"
    # Hostinger defaults — override via EMAIL__SMTP_* env vars.
    smtp_host: str = "smtp.hostinger.com"
    smtp_port: int = 465
    smtp_username: str = "contact@page7.io"
    smtp_password: SecretStr | None = None
    # True = implicit TLS (465). False = STARTTLS (587).
    smtp_use_tls: bool = True


class JobsSettings(BaseModel):
    """Postgres-native job queue settings (architecture/12, ADR-0002)."""

    poll_interval_seconds: float = 1.0
    """Idle wake-up between claim attempts (LISTEN/NOTIFY not built yet)."""

    grace_period_seconds: float = 25.0
    """SIGTERM grace window: wait this long for in-flight jobs before releasing."""

    scheduler_interval_seconds: float = 30.0
    """How often the cron scheduler loop wakes up."""

    stale_lease_check_seconds: float = 30.0
    """How often the stale-lease sweep runs inside each worker process."""


class ObservabilitySettings(BaseModel):
    sentry_dsn: SecretStr | None = None
    otel_enabled: bool = False


class BillingSettings(BaseModel):
    """Phase 13 — seller identity and VAT for issued invoices. Env prefix BILLING__."""

    seller_vat_number: str = "300000000000003"
    """pgblank.ai's own VAT registration number (15 digits), snapshotted onto every invoice."""

    default_vat_rate: float = 0.15
    """Saudi VAT rate stored on each invoice at issue time."""


class Settings(BaseSettings):
    """Nested settings groups exist for every domain from Phase 1 on — mostly empty
    now — so later phases add fields to an already-established group instead of
    inventing a new one. Env vars use `__` to address nested fields, e.g.
    `DATABASE__URL`, `AUTH__TOKEN_ENCRYPTION_KEY`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_env: Environment = Environment.DEVELOPMENT

    # Number of trusted reverse proxies in front of the API (Cloud Run = 1). Used to read
    # the real client IP from X-Forwarded-For; 0 ignores the header. See core/security/client_ip.py.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5)

    # Top-level, not nested under `auth` — architecture/05 §4 fixes these exact
    # env var names: `APP_ENCRYPTION_KEY__<alias>` populates the dict below,
    # `APP_ENCRYPTION_KEY_CURRENT` names the alias new encryptions use. Every
    # write encrypts under the current key and stamps its alias; every read
    # decrypts under whichever alias the row itself names, so rotating the
    # current key never strands an already-encrypted row.
    app_encryption_key: dict[str, SecretStr] = Field(default_factory=dict)
    app_encryption_key_current: str | None = None

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    cors: CORSSettings = Field(default_factory=CORSSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    stock: StockSettings = Field(default_factory=StockSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    imagegen: ImageGenSettings = Field(default_factory=ImageGenSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    social: SocialSettings = Field(default_factory=SocialSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    jobs: JobsSettings = Field(default_factory=JobsSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    billing: BillingSettings = Field(default_factory=BillingSettings)

    @model_validator(mode="after")
    def _fail_closed_outside_development(self) -> Settings:
        """Outside `development`, a missing or placeholder secret is a startup
        error, not a warning — architecture/05 §4. `development` is intentionally
        exempt so a fresh local checkout with no secrets configured still boots.
        """
        if self.auth.cookie_samesite == "none" and self.app_env is Environment.DEVELOPMENT:
            # Secure is required for SameSite=None; development sets Secure=False.
            raise ValueError(
                "AUTH__COOKIE_SAMESITE=none requires Secure cookies, which are "
                "disabled in development"
            )
        if self.app_env is Environment.DEVELOPMENT:
            return self
        current = self.app_encryption_key_current
        if current is None or current.strip().lower() in _PLACEHOLDER_VALUES:
            raise ValueError(
                f"APP_ENCRYPTION_KEY_CURRENT must be set to a real key alias when "
                f"APP_ENV={self.app_env.value!r}"
            )
        key = self.app_encryption_key.get(current)
        if key is None or key.get_secret_value().strip().lower() in _PLACEHOLDER_VALUES:
            raise ValueError(
                f"APP_ENCRYPTION_KEY__{current} must be set to a real secret when "
                f"APP_ENV={self.app_env.value!r}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
