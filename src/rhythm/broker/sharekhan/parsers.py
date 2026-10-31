"""Parsers for Sharekhan API responses, written against the *documented* shapes only.

Source: https://www.sharekhan.com/trading-api/documentation (accessed 2026-10-31); see
docs/SHAREKHAN_INTEGRATION.md. These parsers are UNVERIFIED against live responses: no
real Sharekhan call has been made. They validate strictly and raise BrokerResponseError
on any deviation instead of guessing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from rhythm.broker.base import (
    BrokerAuthError,
    BrokerRateLimitedError,
    BrokerResponseError,
    BrokerUnavailableError,
)
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey, to_decimal
from rhythm.instruments.types import ScripRecord

# Documented exchange codes (Data Type section).
EXCHANGE_TO_SHAREKHAN: Mapping[Exchange, str] = {Exchange.NSE: "NC", Exchange.BSE: "BC"}
SHAREKHAN_TO_EXCHANGE: Mapping[str, Exchange] = {v: k for k, v in EXCHANGE_TO_SHAREKHAN.items()}

# The docs list instType values "such as EQ, FS, FI"; the sample row shows "SM". Which codes
# denote cash equity is a recorded blocker, so the caller must pass an explicit allow-list.
DEFAULT_EQUITY_INST_TYPES: frozenset[str] = frozenset({"EQ"})


def check_envelope(http_status: int, body: Mapping[str, Any] | None) -> Any:
    """Map documented status codes to errors; return ``data`` (or [] for 204)."""
    if http_status == 204:
        return []
    if http_status == 403:
        raise BrokerAuthError("Sharekhan: token expired or access denied (403)")
    if http_status == 429:
        raise BrokerRateLimitedError("Sharekhan: rate limited (429)")
    if http_status in (500, 502):
        raise BrokerUnavailableError(f"Sharekhan: service error ({http_status})")
    if http_status != 200 or body is None:
        raise BrokerResponseError(f"Sharekhan: unexpected HTTP {http_status}")
    if body.get("status") != 200 or "data" not in body:
        raise BrokerResponseError("Sharekhan: envelope missing status=200/data")
    return body["data"]


def _req(row: Mapping[str, Any], key: str) -> Any:
    if key not in row or row[key] is None:
        raise BrokerResponseError(f"scrip master row missing {key!r}")
    return row[key]


def parse_scrip_master(
    http_status: int,
    body: Mapping[str, Any] | None,
    exchange: Exchange,
    *,
    allowed_inst_types: Collection[str] = DEFAULT_EQUITY_INST_TYPES,
) -> list[ScripRecord]:
    data = check_envelope(http_status, body)
    if not isinstance(data, list):
        raise BrokerResponseError("scrip master data is not a list")
    out: list[ScripRecord] = []
    for row in data:
        if not isinstance(row, Mapping):
            raise BrokerResponseError("scrip master row is not an object")
        inst_type = str(_req(row, "instType"))
        if inst_type not in allowed_inst_types:
            continue
        symbol = str(_req(row, "tradingSymbol")).strip().upper()
        try:
            InstrumentKey(exchange, symbol)
            tick = to_decimal(_req(row, "tickSize"))
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise BrokerResponseError(f"invalid scrip master row for {symbol!r}: {exc}") from exc
        lot_raw = int(_req(row, "lotSize"))
        # Documented sample shows lotSize 0 for an equity row; cash equity trades in units of 1.
        lot = 1 if lot_raw <= 0 else lot_raw
        isin = row.get("isinCode") or None
        out.append(
            ScripRecord(
                exchange=exchange,
                symbol=symbol,
                source=DataOrigin.SHAREKHAN,
                source_code=str(_req(row, "scripCode")),
                instrument_type=inst_type,
                tick_size=tick if tick > Decimal(0) else Decimal(0),
                lot_size=lot,
                isin=str(isin) if isin else None,
                name=(str(row["companyName"]) if row.get("companyName") else None),
            )
        )
    return out


def sharekhan_symbols(records: Sequence[ScripRecord]) -> dict[str, str]:
    """Map feed symbol (exchange code + scrip code, e.g. 'NC2885') -> trading symbol."""
    return {f"{EXCHANGE_TO_SHAREKHAN[r.exchange]}{r.source_code}": r.symbol for r in records}
