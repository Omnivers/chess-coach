"""Phase 3 + Phase 5+ — FastAPI on :8787.

Wires every router (review, play, drills, session, coach) onto one app,
backed by one journal and one `EnginePool`. Per ROADMAP §1.3, a single
`StockfishEngine` is NOT thread-safe (see engine.py's docstring) and was
being shared between the in-game opponent and every analyst call
(review/guard/hints/highlights) — this factory now builds an `EnginePool`
(one opponent process, one analyst process, each with its own lock)
instead, so those two jobs can never interleave on one UCI pipe.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api_coach import build_router as build_coach_router
from .api_drills import build_router as build_drills_router
from .api_openings import build_router as build_openings_router
from .api_play import build_router as build_play_router
from .api_review import build_router as build_review_router
from .api_session import build_router as build_session_router
from .enginepool import EnginePool
from .journal import Journal, open_journal, sweep_stale_live_games
from .play import LiveGame


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that always revalidates.

    Starlette's StaticFiles sends ETag and Last-Modified but no
    `Cache-Control`, so browsers fall back to heuristic caching and can
    serve a stylesheet from disk cache for hours without ever asking the
    server. On a localhost app whose CSS and JS are edited while it runs,
    that shows up as "my changes did nothing". `no-cache` does not
    disable caching — it forces a revalidation request, so unchanged
    files still come back as a cheap 304.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


DEFAULT_DB = Path(
    os.environ.get("CHESS_COACH_DB", "data/journal.db")
).expanduser()
DEFAULT_WEB_DIR = Path(
    os.environ.get("CHESS_COACH_WEB_DIR", "web")
).expanduser()

# A live game abandoned this long ago (server crash mid-game, browser tab
# closed) is swept from `live_state` at startup so it stops looking "in
# progress" forever.
STALE_LIVE_GAME_HOURS = 24


def create_app(
    journal: Optional[Journal] = None,
    *,
    pool: Optional[EnginePool] = None,
    web_dir: Optional[Path] = None,
) -> FastAPI:
    """Build the FastAPI app.

    `web_dir` overrides the location of the frontend directory (both the
    single-file `index.html` served at `/` and the `/static` mount).
    """
    j = journal or open_journal(DEFAULT_DB)
    eng_pool = pool or EnginePool.create()
    web = web_dir or DEFAULT_WEB_DIR
    index_path = web / "index.html"

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_LIVE_GAME_HOURS)
            cutoff_iso = cutoff.replace(microsecond=0).isoformat()  # match utc_now_iso()'s format
            with j.transaction() as conn:
                sweep_stale_live_games(conn, older_than_iso=cutoff_iso)
        except Exception:
            pass  # a failed sweep must never block startup
        yield
        try:
            eng_pool.close()
        except Exception:
            pass

    app = FastAPI(title="Chess Coach API", version="0.2", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8787",
            "http://127.0.0.1:8787",
        ],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "schema_version": j.schema_version(),
            "engine": eng_pool.available,
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        if not index_path.exists():
            return HTMLResponse(
                "<h1>frontend missing</h1>"
                "<p>expected at: " + str(index_path) + "</p>",
                status_code=500,
            )
        return HTMLResponse(
            index_path.read_text(),
            headers={"Cache-Control": "no-cache"},
        )

    # Shared across the play and coach routers so a coach chat can ground
    # itself in the same in-memory live game the player is looking at.
    live_games: dict[str, LiveGame] = {}

    app.include_router(build_review_router(j))
    app.include_router(build_play_router(j, eng_pool, live_games))
    app.include_router(build_coach_router(j, eng_pool, live_games))
    app.include_router(build_drills_router(j))
    app.include_router(build_session_router(j))
    app.include_router(build_openings_router())

    # Mount last: an explicit route (like "/") always wins over a mount at
    # the same prefix, but keeping this after the routers makes the
    # ordering intent explicit. Skip — don't crash — if the frontend
    # directory isn't present (e.g. a bare API-only checkout).
    if web.is_dir():
        app.mount("/static", _NoCacheStaticFiles(directory=str(web)), name="static")

    return app


app = create_app()


__all__ = ["create_app", "app"]
