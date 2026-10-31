"""Parser tests use rows shaped like the documented ScripMaster sample (fields as published)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from rhythm.broker.base import (
    BrokerAuthError,
    BrokerRateLimitedError,
    BrokerResponseError,
    BrokerUnavailableError,
)
from rhythm.broker.sharekhan.parsers import (
    check_envelope,
    parse_scrip_master,
    sharekhan_symbols,
)
from rhythm.core.market import DataOrigin, Exchange


def doc_row(**kw: Any) -> dict[str, Any]:
    # Same keys and value types as the documented sample response.
    row: dict[str, Any] = {
        "scripCode": 1,
        "tradingSymbol": "GOLDSTAR",
        "tickSize": 1.0,
        "instType": "SM",
        "lotSize": 0,
        "companyName": "GOLDSTAR POWER LIMITED",
        "indices": "0",
        "groupName": "NA",
        "industry": "NA",
        "isinCode": "INE405Y01013",
        "expiry": None,
        "strike": 0.0,
        "optionType": None,
    }
    row.update(kw)
    return row


def body(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": 200,
        "message": "master",
        "timestamp": "2021-03-02T08:26:24+05:30",
        "data": rows,
    }


def test_unlisted_inst_types_are_skipped_by_default() -> None:
    assert parse_scrip_master(200, body([doc_row()]), Exchange.NSE) == []


def test_documented_row_parses_when_type_allowed() -> None:
    (r,) = parse_scrip_master(200, body([doc_row()]), Exchange.NSE, allowed_inst_types={"SM"})
    assert r.symbol == "GOLDSTAR"
    assert r.source is DataOrigin.SHAREKHAN
    assert r.source_code == "1"
    assert r.tick_size == Decimal("1.0")
    assert r.lot_size == 1  # documented 0 normalised to 1 for cash equity
    assert r.isin == "INE405Y01013"


def test_symbol_feed_mapping() -> None:
    recs = parse_scrip_master(
        200,
        body([doc_row(scripCode=2885, tradingSymbol="ABC")]),
        Exchange.NSE,
        allowed_inst_types={"SM"},
    )
    assert sharekhan_symbols(recs) == {"NC2885": "ABC"}


@pytest.mark.parametrize(
    ("code", "err"),
    [
        (403, BrokerAuthError),
        (429, BrokerRateLimitedError),
        (500, BrokerUnavailableError),
        (502, BrokerUnavailableError),
        (418, BrokerResponseError),
    ],
)
def test_status_code_mapping(code: int, err: type[Exception]) -> None:
    with pytest.raises(err):
        check_envelope(code, {"status": code, "message": "x"})


def test_204_means_no_records() -> None:
    assert check_envelope(204, None) == []


def test_malformed_responses_raise() -> None:
    with pytest.raises(BrokerResponseError):
        check_envelope(200, {"message": "no status"})
    with pytest.raises(BrokerResponseError):
        parse_scrip_master(200, {"status": 200, "data": {"not": "a list"}}, Exchange.NSE)
    bad = doc_row()
    del bad["tradingSymbol"]
    with pytest.raises(BrokerResponseError, match="tradingSymbol"):
        parse_scrip_master(200, body([bad]), Exchange.NSE, allowed_inst_types={"SM"})
    with pytest.raises(BrokerResponseError):
        parse_scrip_master(
            200,
            body([doc_row(tradingSymbol="bad symbol!")]),
            Exchange.NSE,
            allowed_inst_types={"SM"},
        )
