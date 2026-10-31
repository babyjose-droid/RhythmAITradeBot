"""Shared FastAPI dependencies and error mapping."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from rhythm.api.state import AppState
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.db.session import session_scope


def get_state(request: Request) -> AppState:
    st: AppState = request.app.state.rhythm
    return st


def get_session(state: Annotated[AppState, Depends(get_state)]) -> Iterator[Session]:
    with session_scope(state.session_factory) as s:
        yield s


StateDep = Annotated[AppState, Depends(get_state)]
SessionDep = Annotated[Session, Depends(get_session)]


def parse_key(exchange: str, symbol: str) -> InstrumentKey:
    try:
        return InstrumentKey(Exchange(exchange.upper()), symbol.upper())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def problem(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)
