"""Parsers for Sharekhan account reads (funds, holdings, positions, day orders, candles).

Field names come from the documented samples (SHAREKHAN_INTEGRATION.md §5-6); several field
meanings are undocumented, so each parser tries the documented name first and a small set of
alternatives, and raises BrokerResponseError naming the field it could not find. Nothing is
guessed silently. ``shape`` describes a response by field names and JSON types only, never
values, so it can be stored to resolve the open questions without keeping account data.

UNVERIFIED against live responses until the supervised read-only session.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from rhythm.broker.base import (
    BrokerOrderRecord,
    BrokerResponseError,
    FundsSnapshot,
    HoldingRecord,
    PositionRecord,
)
from rhythm.broker.sharekhan.parsers import SHAREKHAN_TO_EXCHANGE
from rhythm.core.market import DataOrigin, Exchange

IST = ZoneInfo("Asia/Kolkata")
SYMBOL_KEYS = ("tradingSymbol", "symbol", "scripName", "tradingsymbol")


def _rows(data: Any, what: str) -> list[Mapping[str, Any]]:
    if data in (None, []):
        return []
    if isinstance(data, Mapping):  # some endpoints may wrap a single object
        data = [data]
    if not isinstance(data, list) or not all(isinstance(r, Mapping) for r in data):
        raise BrokerResponseError(f"{what}: data is not a list of objects")
    return list(data)


def _pick(row: Mapping[str, Any], keys: Sequence[str], what: str) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    raise BrokerResponseError(f"{what}: none of {list(keys)} present")


def _opt(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _dec(v: Any, what: str) -> Decimal:
    try:
        return Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise BrokerResponseError(f"{what}: not a number") from exc


def _int(v: Any, what: str) -> int:
    d = _dec(v, what)
    if d != d.to_integral_value():
        raise BrokerResponseError(f"{what}: not a whole number")
    return int(d)


def _exchange(row: Mapping[str, Any], default: Exchange | None) -> Exchange | None:
    code = _opt(row, ("exchange", "exchangeCode", "exch"))
    if code is None:
        return default
    return SHAREKHAN_TO_EXCHANGE.get(str(code).strip().upper())


@dataclass(frozen=True)
class Parsed[T]:
    records: list[T]
    skipped: int  # rows outside cash equity on NSE/BSE (e.g. derivatives), not shown


def parse_funds(data: Any, exchange: Exchange, as_of: datetime) -> FundsSnapshot:
    rows = _rows(data, "funds")
    if not rows:
        raise BrokerResponseError("funds: empty response")
    row = rows[0]
    cash = _dec(_pick(row, ("currentCashBalance", "cashBalance"), "funds"), "funds cash")
    raw = tuple(
        sorted((str(k), str(v)) for k, v in row.items() if isinstance(v, str | int | float))
    )
    return FundsSnapshot(exchange, cash, DataOrigin.SHAREKHAN, as_of, raw)


def parse_holdings(data: Any, as_of: datetime) -> Parsed[HoldingRecord]:
    out: list[HoldingRecord] = []
    skipped = 0
    for row in _rows(data, "holdings"):
        exch = _exchange(row, Exchange.NSE)
        if exch is None:
            skipped += 1
            continue
        qty_raw = _pick(row, ("qty", "quantity", "dp", "totalQty", "cncqty"), "holdings qty")
        avg = _opt(row, ("holdPrice", "avgPrice", "averagePrice"))
        isin = _opt(row, ("isin", "isinCode"))
        out.append(
            HoldingRecord(
                exchange=exch,
                symbol=str(_pick(row, SYMBOL_KEYS, "holdings symbol")).strip().upper(),
                quantity=_int(qty_raw, "holdings qty"),
                origin=DataOrigin.SHAREKHAN,
                as_of=as_of,
                isin=str(isin) if isin else None,
                average_price=_dec(avg, "holdings price") if avg is not None else None,
            )
        )
    return Parsed(out, skipped)


def parse_positions(data: Any, as_of: datetime) -> Parsed[PositionRecord]:
    out: list[PositionRecord] = []
    skipped = 0
    for row in _rows(data, "positions"):
        exch = _exchange(row, None)
        if exch is None:
            skipped += 1
            continue
        avg = _opt(row, ("avgPrice", "averagePrice", "netPrice"))
        out.append(
            PositionRecord(
                exchange=exch,
                symbol=str(_pick(row, SYMBOL_KEYS, "positions symbol")).strip().upper(),
                product=str(_opt(row, ("productType", "product")) or "UNKNOWN"),
                net_quantity=_int(_pick(row, ("netQty", "netQuantity"), "positions"), "netQty"),
                origin=DataOrigin.SHAREKHAN,
                as_of=as_of,
                average_price=_dec(avg, "positions price") if avg is not None else None,
            )
        )
    return Parsed(out, skipped)


def parse_day_orders(data: Any, as_of: datetime) -> Parsed[BrokerOrderRecord]:
    out: list[BrokerOrderRecord] = []
    skipped = 0
    for row in _rows(data, "orders"):
        exch = _exchange(row, None)
        if exch is None:
            skipped += 1
            continue
        price = _opt(row, ("price", "orderPrice"))
        fill = _opt(row, ("execPrice", "avgPrice"))
        out.append(
            BrokerOrderRecord(
                broker_order_id=str(_pick(row, ("orderId", "exchOrderId"), "orders id")),
                exchange=exch,
                symbol=str(_pick(row, SYMBOL_KEYS, "orders symbol")).strip().upper(),
                side=str(_opt(row, ("transactionType", "buySell")) or "UNKNOWN"),
                quantity=_int(_pick(row, ("orderQty", "quantity", "qty"), "orders qty"), "qty"),
                filled_quantity=_int(_opt(row, ("execQty", "filledQty")) or 0, "execQty"),
                status=str(_opt(row, ("orderStatus", "status")) or "UNKNOWN"),
                origin=DataOrigin.SHAREKHAN,
                as_of=as_of,
                price=_dec(price, "orders price") if price is not None else None,
                average_fill_price=_dec(fill, "orders fill") if fill is not None else None,
            )
        )
    return Parsed(out, skipped)


@dataclass(frozen=True)
class Candle:
    ts: datetime  # IST-aware
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def parse_candles(data: Any) -> list[Candle]:
    out: list[Candle] = []
    for row in _rows(data, "historical"):
        day = str(_pick(row, ("tradeDate",), "historical date")).strip()
        tm = str(row.get("tradeTime") or "00:00:00").strip()
        try:
            ts = datetime.strptime(f"{day} {tm}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            try:
                ts = datetime.strptime(f"{day} {tm}", "%d/%m/%Y %H:%M").replace(tzinfo=IST)
            except ValueError as exc:
                raise BrokerResponseError("historical: unrecognised tradeDate/tradeTime") from exc
        o, h, low, c = (
            _dec(_pick(row, (k,), "historical"), k) for k in ("open", "high", "low", "close")
        )
        if not (low <= min(o, c) and h >= max(o, c)):
            raise BrokerResponseError("historical: candle fails high/low sanity check")
        out.append(Candle(ts, o, h, low, c, _int(row.get("qty") or 0, "qty")))
    out.sort(key=lambda x: x.ts)
    return out


def _type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int | float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def shape(data: Any) -> dict[str, Any]:
    """Field names and JSON types of a response; never values."""
    if isinstance(data, list):
        fields: dict[str, set[str]] = {}
        for row in data:
            if isinstance(row, Mapping):
                for k, v in row.items():
                    fields.setdefault(str(k), set()).add(_type(v))
        return {
            "kind": "array",
            "count": len(data),
            "fields": {k: sorted(v) for k, v in sorted(fields.items())},
        }
    if isinstance(data, Mapping):
        return {"kind": "object", "fields": {str(k): [_type(v)] for k, v in sorted(data.items())}}
    return {"kind": _type(data)}
