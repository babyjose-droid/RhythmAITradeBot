"""Versioned risk policy (RISK_MANAGEMENT.md §3).

All limits are ILLUSTRATIVE PAPER-TEST SETTINGS, NOT RECOMMENDATIONS. Real limits must be set
by the account owner. A policy is immutable; a change creates a new version (stored with who
approved it and why). No LLM component can create or change a policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import time
from decimal import Decimal, InvalidOperation
from typing import Any

from rhythm.core.errors import RhythmError
from rhythm.core.market import Horizon

DEFAULT_VERSION = "paper-default-1"


class PolicyValidationError(RhythmError):
    pass


def _pct(lo: str, hi: str) -> tuple[Decimal, Decimal]:
    return Decimal(lo), Decimal(hi)


# Hard sanity bounds. A policy outside these is rejected, whoever submits it.
_BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "intraday_risk_per_trade_pct": _pct("0.0001", "0.02"),
    "swing_risk_per_trade_pct": _pct("0.0001", "0.02"),
    "daily_loss_stop_pct": _pct("0.001", "0.05"),
    "max_single_name_pct": _pct("0.01", "0.50"),
    "max_sector_pct": _pct("0.05", "1"),
    "max_adv_participation": _pct("0.0001", "0.10"),
    "cash_buffer_pct": _pct("0", "0.05"),
}
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "max_open_orders": (1, 100),
    "max_open_orders_per_strategy": (1, 100),
    "max_orders_per_minute": (1, 120),
    "reconciliation_max_age_seconds": (5, 3600),
}


@dataclass(frozen=True)
class RiskPolicy:
    version: str = DEFAULT_VERSION
    intraday_risk_per_trade_pct: Decimal = Decimal("0.005")  # R7
    swing_risk_per_trade_pct: Decimal = Decimal("0.0075")  # R7
    daily_loss_stop_pct: Decimal = Decimal("0.015")  # R8, of start-of-day equity, per strategy
    max_single_name_pct: Decimal = Decimal("0.10")  # R10, consolidated across horizons
    max_sector_pct: Decimal = Decimal("0.25")  # R11
    max_adv_participation: Decimal = Decimal("0.01")  # R12, of average daily volume
    cash_buffer_pct: Decimal = Decimal("0.003")  # R13 slippage + charges headroom
    max_open_orders: int = 10  # R14, risk-increasing orders
    max_open_orders_per_strategy: int = 5  # R14
    max_orders_per_minute: int = 10  # R15
    reconciliation_max_age_seconds: int = 60  # R3
    entry_window_start: time = time(9, 20)  # R17, exchange-local
    entry_window_end: time = time(14, 45)  # R17
    allowed_instrument_types: tuple[str, ...] = ("EQ",)  # R5
    review_above_order_value: Decimal | None = None  # REQUIRES_REVIEW above this value
    ledger_allocation_pct: dict[str, Decimal] = field(
        default_factory=lambda: {
            Horizon.INTRADAY.value: Decimal("0.30"),
            Horizon.SWING.value: Decimal("0.30"),
            Horizon.LONG_TERM.value: Decimal("0.40"),
        }
    )

    def __post_init__(self) -> None:
        self.validate()

    # ------------------------------------------------------------------ validation
    def validate(self) -> None:
        for name, (lo, hi) in _BOUNDS.items():
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not lo <= v <= hi:
                raise PolicyValidationError(f"{name}={v} outside [{lo}, {hi}]")
        for name, (ilo, ihi) in _INT_BOUNDS.items():
            iv = getattr(self, name)
            if not isinstance(iv, int) or isinstance(iv, bool) or not ilo <= iv <= ihi:
                raise PolicyValidationError(f"{name}={iv} outside [{ilo}, {ihi}]")
        if self.max_open_orders_per_strategy > self.max_open_orders:
            raise PolicyValidationError("per-strategy open orders exceed the global limit")
        if not self.entry_window_start < self.entry_window_end:
            raise PolicyValidationError("entry window start must be before its end")
        if not self.allowed_instrument_types:
            raise PolicyValidationError("at least one instrument type must be allowed")
        if self.review_above_order_value is not None and self.review_above_order_value <= 0:
            raise PolicyValidationError("review threshold must be positive")
        alloc = self.ledger_allocation_pct
        if set(alloc) != {h.value for h in Horizon}:
            raise PolicyValidationError("ledger allocation must cover every horizon")
        if any(v < 0 for v in alloc.values()) or sum(alloc.values(), Decimal(0)) > 1:
            raise PolicyValidationError("ledger allocation must be non-negative and sum to <= 1")
        if not self.version or len(self.version) > 40:
            raise PolicyValidationError("version must be 1-40 characters")

    def risk_per_trade(self, horizon: Horizon) -> Decimal | None:
        if horizon is Horizon.INTRADAY:
            return self.intraday_risk_per_trade_pct
        if horizon is Horizon.SWING:
            return self.swing_risk_per_trade_pct
        return None  # long-term sizing uses allocation bands (Phase 6)

    # ------------------------------------------------------------------ (de)serialisation
    def to_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if k == "version":
                continue
            if isinstance(v, time):
                out[k] = v.strftime("%H:%M")
            elif isinstance(v, tuple):
                out[k] = list(v)
            elif isinstance(v, dict):
                out[k] = {kk: str(vv) for kk, vv in v.items()}
            elif v is None or isinstance(v, int):
                out[k] = v
            else:
                out[k] = str(v)
        return out

    def describe(self) -> dict[str, Any]:
        return {"version": self.version, **self.to_params()}

    @classmethod
    def from_params(cls, version: str, params: dict[str, Any]) -> RiskPolicy:
        known = {f.name: f for f in fields(cls)}
        unknown = set(params) - set(known) - {"version"}
        if unknown:
            raise PolicyValidationError(f"unknown policy fields: {sorted(unknown)}")
        base = cls()
        kwargs: dict[str, Any] = {"version": version}
        try:
            for k, raw in params.items():
                if k == "version":
                    continue
                default = getattr(base, k)
                if isinstance(default, time):
                    hh, mm = str(raw).split(":")
                    kwargs[k] = time(int(hh), int(mm))
                elif isinstance(default, tuple):
                    kwargs[k] = tuple(str(x) for x in raw)
                elif isinstance(default, dict):
                    kwargs[k] = {str(kk): Decimal(str(vv)) for kk, vv in raw.items()}
                elif isinstance(default, bool):
                    raise PolicyValidationError(f"{k}: unsupported type")
                elif isinstance(default, int):
                    if isinstance(raw, bool) or int(raw) != raw:
                        raise PolicyValidationError(f"{k} must be an integer")
                    kwargs[k] = int(raw)
                elif k == "review_above_order_value":
                    kwargs[k] = None if raw is None else Decimal(str(raw))
                else:
                    kwargs[k] = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
            raise PolicyValidationError(f"invalid policy value: {exc}") from exc
        return replace(base, **kwargs)


DEFAULT_POLICY = RiskPolicy()
