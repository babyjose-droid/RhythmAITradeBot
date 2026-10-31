"""Watchlist management. Every mutation writes an audit event in the same transaction."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from rhythm.audit import ActorType, AuditLog
from rhythm.core.errors import RhythmError
from rhythm.core.market import InstrumentKey
from rhythm.db.models import Watchlist, WatchlistItem
from rhythm.instruments.registry import get_active

WATCHLIST_HORIZONS = ("INTRADAY", "SWING", "LONG_TERM", "ANY")


class WatchlistError(RhythmError):
    pass


class NotFoundError(WatchlistError):
    pass


class ConflictError(WatchlistError):
    pass


def list_watchlists(session: Session) -> list[Watchlist]:
    stmt = select(Watchlist).options(selectinload(Watchlist.items)).order_by(Watchlist.name)
    return list(session.execute(stmt).scalars())


def get_watchlist(session: Session, watchlist_id: int) -> Watchlist:
    wl = session.execute(
        select(Watchlist).options(selectinload(Watchlist.items)).where(Watchlist.id == watchlist_id)
    ).scalar_one_or_none()
    if wl is None:
        raise NotFoundError(f"watchlist {watchlist_id} not found")
    return wl


def create_watchlist(
    session: Session,
    audit: AuditLog,
    *,
    name: str,
    horizon: str,
    description: str | None,
    actor_id: str,
) -> Watchlist:
    if horizon not in WATCHLIST_HORIZONS:
        raise WatchlistError(f"horizon must be one of {WATCHLIST_HORIZONS}")
    if session.execute(select(Watchlist.id).where(Watchlist.name == name)).first():
        raise ConflictError(f"watchlist {name!r} already exists")
    wl = Watchlist(name=name, horizon=horizon, description=description)
    session.add(wl)
    session.flush()
    audit.append(
        session,
        event_type="watchlist.created",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="watchlist",
        subject_id=str(wl.id),
        payload={"name": name, "horizon": horizon},
    )
    return wl


def add_item(
    session: Session,
    audit: AuditLog,
    *,
    watchlist_id: int,
    key: InstrumentKey,
    note: str | None,
    actor_id: str,
) -> WatchlistItem:
    wl = get_watchlist(session, watchlist_id)
    if get_active(session, key) is None:
        raise NotFoundError(f"{key} is not an active instrument in the registry")
    if any(i.exchange == key.exchange.value and i.symbol == key.symbol for i in wl.items):
        raise ConflictError(f"{key} is already in watchlist {wl.name!r}")
    item = WatchlistItem(exchange=key.exchange.value, symbol=key.symbol, note=note)
    wl.items.append(item)
    session.flush()
    audit.append(
        session,
        event_type="watchlist.item_added",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="watchlist",
        subject_id=str(wl.id),
        payload={"instrument": str(key)},
    )
    return item


def remove_item(
    session: Session, audit: AuditLog, *, watchlist_id: int, key: InstrumentKey, actor_id: str
) -> None:
    wl = get_watchlist(session, watchlist_id)
    item = next(
        (i for i in wl.items if i.exchange == key.exchange.value and i.symbol == key.symbol), None
    )
    if item is None:
        raise NotFoundError(f"{key} is not in watchlist {wl.name!r}")
    wl.items.remove(item)
    session.flush()
    audit.append(
        session,
        event_type="watchlist.item_removed",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="watchlist",
        subject_id=str(wl.id),
        payload={"instrument": str(key)},
    )


def delete_watchlist(
    session: Session, audit: AuditLog, *, watchlist_id: int, actor_id: str
) -> None:
    wl = get_watchlist(session, watchlist_id)
    payload = {"name": wl.name, "items": [f"{i.exchange}:{i.symbol}" for i in wl.items]}
    session.delete(wl)
    session.flush()
    audit.append(
        session,
        event_type="watchlist.deleted",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="watchlist",
        subject_id=str(watchlist_id),
        payload=payload,
    )
