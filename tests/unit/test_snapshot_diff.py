from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from rhythm.broker.mock import mock_scrip_master
from rhythm.core.market import Exchange
from rhythm.instruments.snapshot import SnapshotRejectedError, content_hash, diff_snapshot

BASE = mock_scrip_master(Exchange.NSE)


def _current(records: list) -> dict:  # type: ignore[type-arg]
    return {r.symbol: r.attributes() for r in records}


def test_first_snapshot_adds_everything() -> None:
    d = diff_snapshot({}, BASE, Exchange.NSE)
    assert d.summary() == {"added": 20, "changed": 0, "removed": 0, "unchanged": 0}


def test_unchanged_snapshot() -> None:
    d = diff_snapshot(_current(BASE), BASE, Exchange.NSE)
    assert d.summary() == {"added": 0, "changed": 0, "removed": 0, "unchanged": 20}


def test_add_change_remove() -> None:
    incoming = [replace(BASE[0], tick_size=Decimal("0.10")), *BASE[2:]]
    incoming.append(replace(BASE[0], symbol="MOCKNEW", source_code="999"))
    d = diff_snapshot(_current(BASE), incoming, Exchange.NSE)
    assert [r.symbol for r in d.changed] == ["MOCKA"]
    assert d.removed == ["MOCKB"]
    assert [r.symbol for r in d.added] == ["MOCKNEW"]


def test_empty_snapshot_refused() -> None:
    with pytest.raises(SnapshotRejectedError, match="empty"):
        diff_snapshot(_current(BASE), [], Exchange.NSE)


def test_duplicates_and_bad_rows_refused() -> None:
    with pytest.raises(SnapshotRejectedError, match="invalid rows") as exc:
        diff_snapshot({}, [BASE[0], BASE[0], replace(BASE[1], tick_size=Decimal(0))], Exchange.NSE)
    problems = str(exc.value.detail["problems"])
    assert "duplicate" in problems
    assert "tick" in problems


def test_wrong_exchange_refused() -> None:
    with pytest.raises(SnapshotRejectedError):
        diff_snapshot({}, mock_scrip_master(Exchange.BSE), Exchange.NSE)


def test_mass_removal_requires_review() -> None:
    with pytest.raises(SnapshotRejectedError, match="too many removals"):
        diff_snapshot(_current(BASE), BASE[:10], Exchange.NSE)  # 50% would disappear
    d = diff_snapshot(_current(BASE), BASE[:10], Exchange.NSE, max_removal_ratio=0.6)
    assert len(d.removed) == 10


def test_content_hash_is_order_independent() -> None:
    assert content_hash(BASE) == content_hash(list(reversed(BASE)))
    assert content_hash(BASE) != content_hash(BASE[1:])
