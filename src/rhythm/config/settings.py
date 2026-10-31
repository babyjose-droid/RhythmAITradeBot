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
            if "mock" in (self.market_data_provider, self.broker_adapter):
                raise ValueError("mock data sources are not allowed outside local/test")
        if self.features.paper_trading and self.trading_mode is not TradingMode.PAPER:
            raise ValueError("paper_trading feature requires trading_mode=PAPER")
        return self

    @property
    def orders_permitted(self) -> bool:
        """Whether any (simulated) order may be created. Never true for real money in Phase 1."""
        return self.trading_mode is TradingMode.PAPER


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
