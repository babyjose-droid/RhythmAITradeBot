"""Watchlist endpoints. Mutations are audited; actor is the local owner until auth (Phase 8)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from rhythm.api.deps import SessionDep, StateDep, parse_key
from rhythm.db.models import Watchlist
from rhythm.instruments import watchlists as svc
from rhythm.instruments.registry import get_active

router = APIRouter(prefix="/v1/watchlists", tags=["watchlists"])


class WatchlistIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    horizon: str = Field(pattern="^(INTRADAY|SWING|LONG_TERM|ANY)$")
    description: str | None = Field(default=None, max_length=500)


class ItemIn(BaseModel):
    instrument: str = Field(description="EXCHANGE:SYMBOL, e.g. NSE:MOCKA", max_length=48)
    note: str | None = Field(default=None, max_length=500)


class ItemOut(BaseModel):
    instrument: str
    note: str | None
    added_at: datetime
    active_in_registry: bool


class WatchlistOut(BaseModel):
    id: int
    name: str
    horizon: str
    description: str | None
    items: list[ItemOut]


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except svc.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except svc.ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except svc.WatchlistError as exc:
        raise HTTPException(422, str(exc)) from exc


def _out(session: SessionDep, wl: Watchlist) -> WatchlistOut:
    items = []
    for i in wl.items:
        key = parse_key(i.exchange, i.symbol)
        items.append(
            ItemOut(
                instrument=str(key),
                note=i.note,
                added_at=i.added_at,
                active_in_registry=get_active(session, key) is not None,
            )
        )
    return WatchlistOut(
        id=wl.id, name=wl.name, horizon=wl.horizon, description=wl.description, items=items
    )


@router.get("", response_model=list[WatchlistOut])
def list_all(session: SessionDep) -> list[WatchlistOut]:
    return [_out(session, wl) for wl in svc.list_watchlists(session)]


@router.post("", response_model=WatchlistOut, status_code=status.HTTP_201_CREATED)
def create(body: WatchlistIn, state: StateDep, session: SessionDep) -> WatchlistOut:
    with _errors():
        wl = svc.create_watchlist(
            session,
            state.audit,
            name=body.name,
            horizon=body.horizon,
            description=body.description,
            actor_id=state.settings.local_actor_id,
        )
        return _out(session, svc.get_watchlist(session, wl.id))


@router.get("/{watchlist_id}", response_model=WatchlistOut)
def get_one(watchlist_id: int, session: SessionDep) -> WatchlistOut:
    with _errors():
        return _out(session, svc.get_watchlist(session, watchlist_id))


@router.delete("/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(watchlist_id: int, state: StateDep, session: SessionDep) -> Response:
    with _errors():
        svc.delete_watchlist(
            session, state.audit, watchlist_id=watchlist_id, actor_id=state.settings.local_actor_id
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{watchlist_id}/items", response_model=WatchlistOut, status_code=201)
def add_item(watchlist_id: int, body: ItemIn, state: StateDep, session: SessionDep) -> WatchlistOut:
    exchange, _, symbol = body.instrument.partition(":")
    key = parse_key(exchange, symbol)
    with _errors():
        svc.add_item(
            session,
            state.audit,
            watchlist_id=watchlist_id,
            key=key,
            note=body.note,
            actor_id=state.settings.local_actor_id,
        )
        return _out(session, svc.get_watchlist(session, watchlist_id))


@router.delete("/{watchlist_id}/items/{exchange}/{symbol}", status_code=204)
def remove_item(
    watchlist_id: int, exchange: str, symbol: str, state: StateDep, session: SessionDep
) -> Response:
    key = parse_key(exchange, symbol)
    with _errors():
        svc.remove_item(
            session,
            state.audit,
            watchlist_id=watchlist_id,
            key=key,
            actor_id=state.settings.local_actor_id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
