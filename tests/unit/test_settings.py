from __future__ import annotations

import pytest
from pydantic import ValidationError

from rhythm.config import LIVE_TRADING_IMPLEMENTED, Settings, TradingMode
from tests.conftest import make_settings


def test_default_mode_is_research_only() -> None:
    s = make_settings()
    assert s.trading_mode is TradingMode.RESEARCH_ONLY
    assert s.orders_permitted is False


def test_all_feature_flags_default_off() -> None:
    assert make_settings().features.model_dump() == {
        "broker_read_only": False,
        "research_service": False,
        "paper_trading": False,
    }


def test_live_mode_is_refused() -> None:
    with pytest.raises(ValidationError, match="LIVE trading mode is not available"):
        make_settings(trading_mode="LIVE")


def test_live_mode_is_refused_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHYTHM_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("RHYTHM_TRADING_MODE", "LIVE")
    with pytest.raises(ValidationError, match="LIVE"):
        Settings(_env_file=None)


def test_live_trading_not_implemented_guard() -> None:
    # Changing this constant is a Phase 10 decision and must also change this test.
    assert LIVE_TRADING_IMPLEMENTED is False


def test_paper_mode_permits_simulated_orders() -> None:
    s = make_settings(trading_mode="PAPER")
    assert s.orders_permitted is True


def test_paper_feature_requires_paper_mode() -> None:
    with pytest.raises(ValidationError, match="paper_trading feature requires"):
        make_settings(features={"paper_trading": True})
    assert make_settings(
        trading_mode="PAPER", features={"paper_trading": True}
    ).features.paper_trading


def test_unknown_feature_flag_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(features={"auto_live": True})


@pytest.mark.parametrize("env", ["staging", "production"])
def test_placeholder_credentials_refused_outside_local(env: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(
            environment=env, database_url="postgresql+psycopg://rhythm:change-me@db:5432/rhythm"
        )


def test_production_with_non_placeholder_url_accepted() -> None:
    s = make_settings(
        environment="production", database_url="postgresql+psycopg://rhythm:Xq9-long@db/rhythm"
    )
    assert s.environment.value == "production"


def test_non_postgres_url_refused() -> None:
    with pytest.raises(ValidationError, match="postgresql"):
        make_settings(database_url="sqlite:///x.db")


def test_invalid_log_level_refused() -> None:
    with pytest.raises(ValidationError):
        make_settings(log_level="LOUD")


def test_settings_are_immutable() -> None:
    s = make_settings()
    with pytest.raises(ValidationError):
        s.trading_mode = TradingMode.PAPER  # type: ignore[misc]


def test_database_password_not_in_repr() -> None:
    s = make_settings(database_url="postgresql+psycopg://u:super-secret-pw@h/db")
    assert "super-secret-pw" not in repr(s)
    assert "super-secret-pw" not in str(s.model_dump())


@pytest.mark.parametrize("skew", [0, -1, 61])
def test_clock_skew_bounds(skew: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(max_clock_skew_seconds=skew)
