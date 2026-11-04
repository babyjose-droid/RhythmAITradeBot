"""Application settings and feature flags.

Safety rules enforced here:
* Trading mode defaults to RESEARCH_ONLY.
* LIVE mode is refused — it is not implemented before the Phase 10 activation gate.
* Settings are immutable at runtime; changing mode requires a restart (and, later, an
  audited authorisation flow).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Compile-time style guard: flipping this is a Phase 10 decision, not a configuration change.
LIVE_TRADING_IMPLEMENTED = False

PLACEHOLDER_MARKERS = ("change-me", "changeme", "placeholder", "example")


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class TradingMode(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    PAPER = "PAPER"
    LIVE = "LIVE"


class FeatureFlags(BaseModel):
    """Capabilities that later phases switch on. All off in Phase 1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker_read_only: bool = False
    research_service: bool = False
    paper_trading: bool = False


class ResearchSettings(BaseModel):
    """Claude research service (Phase 7). Advisory only; off unless configured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Model id for the Anthropic provider. No default: the owner chooses it (decision D-08).
    model: str | None = Field(default=None, max_length=100)
    api_key: SecretStr | None = None  # RHYTHM_RESEARCH__API_KEY; from a secret store, never a file
    base_url: str = "https://api.anthropic.com"
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_output_tokens: int = Field(default=1500, ge=256, le=8000)
    # Budget (D-08): tokens per IST calendar day across all assessments; calls that could
    # exceed it are not made. Cost is reported only when the owner sets current rates.
    daily_token_budget: int = Field(default=200_000, ge=0, le=50_000_000)
    max_assessments_per_cycle: int = Field(default=10, ge=0, le=100)
    input_usd_per_mtok: float | None = Field(default=None, ge=0)
    output_usd_per_mtok: float | None = Field(default=None, ge=0)
    # When true, PAPER entries/adds need a valid CONFIRM; anything else holds them (fail closed).
    gate_paper_entries: bool = False

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("research base_url must use https")
        return v.rstrip("/")


class AuthSettings(BaseModel):
    """Dashboard/API sign-in (Phase 8): password + TOTP authenticator code, cookie sessions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    # Encrypts TOTP secrets at rest and must stay stable. From a secret store, >= 32 chars.
    secret_key: SecretStr | None = None
    session_idle_minutes: int = Field(default=30, ge=5, le=480)
    session_absolute_hours: int = Field(default=12, ge=1, le=72)
    reauth_minutes: int = Field(default=5, ge=1, le=30)
    max_failed_logins: int = Field(default=5, ge=3, le=20)
    lockout_minutes: int = Field(default=15, ge=1, le=1440)
    # None = Secure cookies everywhere except environment=local (plain http on 127.0.0.1).
    cookie_secure: bool | None = None


class NotificationSettings(BaseModel):
    """In-app notifications (always on) plus an optional outgoing webhook (Phase 8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    webhook_url: SecretStr | None = None  # e.g. a Slack incoming webhook; https only
    webhook_min_severity: Literal["info", "warning", "critical"] = "warning"
    webhook_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    dispatch_interval_seconds: float = Field(default=10.0, ge=1, le=600)
    max_attempts: int = Field(default=5, ge=1, le=20)

    @field_validator("webhook_url")
    @classmethod
    def _https_only(cls, v: SecretStr | None) -> SecretStr | None:
        if (
            v is not None
            and v.get_secret_value()
            and not v.get_secret_value().startswith("https://")
        ):
            raise ValueError("notification webhook_url must use https")
        return v


class SchedulerSettings(BaseModel):
    """Built-in job scheduler (Phase 9). Jobs are rows in ``scheduled_jobs``; none run unless
    an owner creates and enables them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # True when a scheduler process is deployed: readiness then requires a fresh heartbeat.
    expected: bool = False
    tick_seconds: float = Field(default=15.0, ge=1, le=300)
    heartbeat_stale_seconds: int = Field(default=120, ge=30, le=3600)
    max_consecutive_failures: int = Field(default=3, ge=1, le=20)
    # A slot missed by more than this (scheduler down) is recorded as MISSED, not run late.
    missed_grace_minutes: int = Field(default=10, ge=1, le=240)


class MetricsSettings(BaseModel):
    """Prometheus-style ``/metrics``. Without a token it is served only in local/test."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: SecretStr | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RHYTHM_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.LOCAL
    trading_mode: TradingMode = TradingMode.RESEARCH_ONLY
    database_url: SecretStr
    log_level: str = "INFO"
    log_json: bool = True
    max_clock_skew_seconds: float = Field(default=2.0, gt=0, le=60)
    features: FeatureFlags = FeatureFlags()
    # Phase 2: data sources. "none" means no source configured -> data reported MISSING.
    market_data_provider: Literal["none", "mock"] = "none"
    broker_adapter: Literal["none", "mock"] = "none"
    event_provider: Literal["none", "mock"] = "none"  # corporate-event calendar (R18)
    fundamentals_provider: Literal["none", "mock"] = "none"  # long-term evidence (R21)
    research_provider: Literal["none", "mock", "anthropic"] = "none"  # Phase 7, advisory
    research: ResearchSettings = ResearchSettings()
    auth: AuthSettings = AuthSettings()
    notifications: NotificationSettings = NotificationSettings()
    scheduler: SchedulerSettings = SchedulerSettings()
    metrics: MetricsSettings = MetricsSettings()
    freshness_intraday_max_age_seconds: float = Field(default=10.0, gt=0, le=300)
    # Identity recorded in audit events for local, unauthenticated use (auth arrives in Phase 8).
    local_actor_id: str = Field(default="local-owner", min_length=1, max_length=100)

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"invalid log level {v!r}")
        return v

    @field_validator("database_url")
    @classmethod
    def _postgres_only(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use the postgresql+psycopg:// driver")
        return v

    @model_validator(mode="after")
    def _safety(self) -> Self:
        if self.trading_mode is TradingMode.LIVE and not LIVE_TRADING_IMPLEMENTED:
            raise ValueError(
                "LIVE trading mode is not available. It is locked until the Phase 10 "
                "live-readiness review and explicit owner authorisation."
            )
        if self.environment in (Environment.STAGING, Environment.PRODUCTION):
            url = self.database_url.get_secret_value().lower()
            if any(marker in url for marker in PLACEHOLDER_MARKERS):
                raise ValueError(
                    "placeholder database credentials are not allowed outside local/test"
                )
            if "mock" in (
                self.market_data_provider,
                self.broker_adapter,
                self.event_provider,
                self.fundamentals_provider,
                self.research_provider,
            ):
                raise ValueError("mock data sources are not allowed outside local/test")
        if self.environment in (Environment.STAGING, Environment.PRODUCTION):
            if not self.auth.enabled:
                raise ValueError("authentication cannot be disabled outside local/test")
            if self.auth.cookie_secure is False:
                raise ValueError("insecure session cookies are not allowed outside local/test")
        if self.auth.enabled:
            key = self.auth.secret_key.get_secret_value() if self.auth.secret_key else ""
            if len(key) < 32 or any(m in key.lower() for m in PLACEHOLDER_MARKERS):
                raise ValueError(
                    "authentication needs RHYTHM_AUTH__SECRET_KEY (at least 32 random characters, "
                    "from a secret store); or set RHYTHM_AUTH__ENABLED=false for a local-only demo"
                )
        if self.research_provider != "none" and not self.features.research_service:
            raise ValueError("research_provider requires the research_service feature flag")
        if self.research_provider == "anthropic":
            key = self.research.api_key.get_secret_value() if self.research.api_key else ""
            if not key or any(m in key.lower() for m in PLACEHOLDER_MARKERS):
                raise ValueError(
                    "research_provider=anthropic needs a real RHYTHM_RESEARCH__API_KEY"
                )
            if not self.research.model:
                raise ValueError("research_provider=anthropic needs RHYTHM_RESEARCH__MODEL")
        if self.research.gate_paper_entries and self.research_provider == "none":
            raise ValueError("research gate_paper_entries needs a research_provider")
        if self.features.paper_trading and self.trading_mode is not TradingMode.PAPER:
            raise ValueError("paper_trading feature requires trading_mode=PAPER")
        return self

    @property
    def cookie_secure(self) -> bool:
        if self.auth.cookie_secure is not None:
            return self.auth.cookie_secure
        return self.environment is not Environment.LOCAL

    @property
    def orders_permitted(self) -> bool:
        """Whether any (simulated) order may be created. Never true for real money in Phase 1."""
        return self.trading_mode is TradingMode.PAPER


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
