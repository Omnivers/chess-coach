"""Phase 5.3 — the opening-principles primer (API.md §7.8). Read-only.

This is the one router in the app that never touches a `Journal` or an
`EnginePool`: the content is a fixed, curated table replayed through
python-chess at import time (see `openings.py`'s docstring), so there is
nothing here to look up, nothing to go stale, and nothing that depends on
Stockfish being on PATH. `/openings/primer` behaves identically whether
the engine is up or down — the one API surface in the app with that
property.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .openings import get_primer


class PrincipleOut(BaseModel):
    id: str
    title: str
    body: str


class PrimerPlyOut(BaseModel):
    ply: int
    san: str
    fen: str
    side: str
    note: Optional[str]
    principle_id: Optional[str]


class PrimerLineOut(BaseModel):
    id: str
    title: str
    subtitle: str
    perspective: str
    start_fen: str
    plies: list[PrimerPlyOut]


class PrimerOut(BaseModel):
    principles: list[PrincipleOut]
    lines: list[PrimerLineOut]


def build_router() -> APIRouter:
    router = APIRouter(prefix="/openings", tags=["openings"])

    @router.get("/primer", response_model=PrimerOut)
    def primer() -> dict:
        return get_primer()

    return router


__all__ = ["build_router"]
