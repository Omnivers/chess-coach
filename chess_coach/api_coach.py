"""Phase 6 — the Hermes proxy. The ONLY module allowed to talk to an LLM.

Layer discipline (PROJECT.md §3, API.md §1): every other module in this
app is Truth (evaluates positions, never a network call) or Data (reads/
writes the journal, never evaluates). This module is Coach: it turns
engine-derived facts plus the user's own words into prose, and never the
reverse — a model's reply here is display-only, never parsed back into a
move or acted on.

SSRF boundary (non-negotiable): the upstream base URL comes from the
`HERMES_API_BASE` environment variable ONLY. No request body field is
ever read into a URL, appended to one, or used to redirect one. The
bearer token is read from `HERMES_API_KEY`/`API_SERVER_KEY` (process env,
then `~/.hermes/.env`, mirroring the exact lookup order already used by
`ingest.py::_load_token_from_env`) and is never logged, echoed, or placed
in an error message — only ever sent in the outbound Authorization header.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import chess
import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .analysis import detect_motifs_before_move
from .coach_text import local_review_summary
from .engine import DEFAULT_DEPTH
from .enginepool import EnginePool
from .journal import Journal
from .play import LiveGame

DEFAULT_BASE_URL = "http://127.0.0.1:8642/v1"
STATUS_CACHE_TTL_S = 30.0
AVAILABILITY_TIMEOUT_S = 1.5
CHAT_TIMEOUT_S = 30.0


def _base_url() -> str:
    """Upstream base URL — env only. Never derived from a request."""
    return os.environ.get("HERMES_API_BASE", DEFAULT_BASE_URL)


def _load_token() -> Optional[str]:
    """Bearer token: process env (HERMES_API_KEY, then API_SERVER_KEY),
    else the same two keys in `~/.hermes/.env`. Same lookup pattern as
    `ingest.py::_load_token_from_env`, generalized to two candidate keys.
    """
    for key in ("HERMES_API_KEY", "API_SERVER_KEY"):
        val = os.environ.get(key)
        if val:
            return val
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in ("HERMES_API_KEY", "API_SERVER_KEY"):
            return value.strip().strip('"').strip("'")
    return None


_status_cache: dict = {"checked_at": 0.0, "available": False, "reason": "not checked yet"}


async def _check_availability() -> tuple[bool, str]:
    """A real, cheap reachability probe — cached for STATUS_CACHE_TTL_S.

    The gateway is very likely not running in most environments; that is
    an entirely normal, honestly-reported state, not an error.
    """
    now = time.monotonic()
    if now - _status_cache["checked_at"] < STATUS_CACHE_TTL_S:
        return _status_cache["available"], _status_cache["reason"]

    token = _load_token()
    if token is None:
        available, reason = False, "no Hermes API key configured (set HERMES_API_KEY or API_SERVER_KEY)"
    else:
        base = _base_url()
        try:
            async with httpx.AsyncClient(timeout=AVAILABILITY_TIMEOUT_S) as client:
                resp = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {token}"})
            if resp.status_code < 400:
                available, reason = True, "gateway reachable"
            else:
                available, reason = False, f"gateway responded with HTTP {resp.status_code}"
        except httpx.HTTPError:
            available, reason = False, "gateway not reachable (connection failed or timed out)"

    _status_cache["checked_at"] = now
    _status_cache["available"] = available
    _status_cache["reason"] = reason
    return available, reason


class CoachStatus(BaseModel):
    available: bool
    reason: str
    base_url: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    game_id: Optional[str] = None


def _top_recurring_motifs(journal: Journal, limit: int = 3) -> list[tuple[str, int]]:
    """The user's most common motifs across instructive mistakes, for grounding."""
    with journal.read() as conn:
        rows = conn.execute(
            "SELECT motifs FROM mistakes WHERE instructive = 1 AND motifs IS NOT NULL AND motifs != ''"
        ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for m in (r["motifs"] or "").split(","):
            m = m.strip()
            if m:
                counts[m] = counts.get(m, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def _position_facts(pool: EnginePool, lg: Optional[LiveGame]) -> dict:
    """Current FEN + engine eval/PV/motifs for the live game, when there is one."""
    if lg is None:
        return {}
    fen = lg.board.fen()
    try:
        lines = pool.analyst_analyse(fen, depth=DEFAULT_DEPTH, multipv=1)
    except Exception:
        lines = []
    if not lines:
        return {"fen": fen}
    line = lines[0]
    motifs: list[str] = []
    try:
        best_move = chess.Move.from_uci(line.best_uci)
        motifs = detect_motifs_before_move(lg.board.copy(), best_move, line.best_uci)
    except (ValueError, chess.InvalidMoveError):
        pass
    return {
        "fen": fen, "eval_cp": line.cp, "eval_mate": line.mate,
        "pv": line.pv, "best_uci": line.best_uci, "motifs": motifs,
    }


def _build_system_message(journal: Journal, pool: EnginePool, lg: Optional[LiveGame]) -> str:
    """Ground the model in facts it must treat as sole truth about the position."""
    facts = _position_facts(pool, lg)
    recurring = _top_recurring_motifs(journal)

    lines = [
        "You are a chess coach explaining a position to a student. "
        "You do not evaluate positions yourself — the engine facts below are "
        "the SOLE source of truth about who stands better and what the best "
        "move is. Your job is to EXPLAIN, in plain language, never to "
        "second-guess the engine's assessment. Your reply is prose shown to "
        "the student; it is never parsed as a move or executed.",
    ]
    if facts.get("fen"):
        lines.append(f"Current position (FEN): {facts['fen']}")
    if "eval_cp" in facts:
        eval_txt = f"mate in {facts['eval_mate']}" if facts.get("eval_mate") else f"{facts['eval_cp']} centipawns"
        lines.append(f"Engine evaluation (side to move): {eval_txt}.")
    if facts.get("pv"):
        lines.append(f"Engine's best line: {' '.join(facts['pv'])}.")
    if facts.get("motifs"):
        lines.append(f"Detected motifs in the best line: {', '.join(facts['motifs'])}.")
    if recurring:
        motif_txt = ", ".join(f"{m} ({n}x)" for m, n in recurring)
        lines.append(f"This student's recurring weak motifs across past games: {motif_txt}.")
    return "\n".join(lines)


def build_router(journal: Journal, pool: EnginePool, live_games: dict[str, LiveGame]) -> APIRouter:
    r = APIRouter()

    @r.get("/coach/status", response_model=CoachStatus)
    async def coach_status() -> CoachStatus:
        available, reason = await _check_availability()
        return CoachStatus(available=available, reason=reason, base_url=_base_url())

    @r.post("/coach/chat")
    async def coach_chat(req: ChatRequest) -> StreamingResponse:
        async def gen() -> AsyncIterator[str]:
            available, reason = await _check_availability()
            if not available:
                yield _sse({"delta": f"The coach is offline right now ({reason}). Local analysis, hints, and review are unaffected."})
                yield "data: [DONE]\n\n"
                return

            lg = live_games.get(req.game_id) if req.game_id else None
            system_message = _build_system_message(journal, pool, lg)
            payload = {
                "model": os.environ.get("HERMES_MODEL", "hermes"),
                "stream": True,
                "messages": [{"role": "system", "content": system_message}]
                + [m.model_dump() for m in req.messages],
            }
            headers = {"Content-Type": "application/json"}
            token = _load_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"

            try:
                async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                    async with client.stream(
                        "POST", f"{_base_url()}/chat/completions", json=payload, headers=headers,
                    ) as resp:
                        if resp.status_code >= 400:
                            yield _sse({"delta": f"The coach gateway returned an error (HTTP {resp.status_code})."})
                            yield "data: [DONE]\n\n"
                            return
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                                delta = obj["choices"][0]["delta"].get("content")
                            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                                continue
                            if delta:
                                yield _sse({"delta": delta})
            except httpx.HTTPError:
                yield _sse({"delta": "Could not reach the coach gateway. Local analysis still works."})
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return r


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def generate_review_summary(
    *,
    accuracy: float,
    acpl: Optional[int],
    n_mistakes: int,
    n_highlights: int,
    worst_severity: Optional[str],
) -> tuple[str, str]:
    """Reusable helper for `api_review.py`'s `/summary` endpoint.

    Tries Hermes for a richer summary; falls back to the deterministic
    local template on any unavailability or failure. This is the ONLY
    function outside this module's own router that other modules should
    call to get coach-generated text — it still makes the sole HTTP call
    to Hermes itself, so the "only api_coach.py talks to an LLM" rule
    holds even though `api_review.py` triggers it.

    Returns (text, source) where source is "hermes" or "local" so the
    caller can be transparent with the client about where the text
    came from.
    """
    fallback = local_review_summary(
        accuracy=accuracy, acpl=acpl, n_mistakes=n_mistakes,
        n_highlights=n_highlights, worst_severity=worst_severity,
    )
    available, _ = await _check_availability()
    if not available:
        return fallback, "local"

    token = _load_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    prompt = (
        "Write a short (2-4 sentence), encouraging but honest summary of this "
        f"chess game for the player. Accuracy: {accuracy:.1f}%. "
        f"Average centipawn loss: {acpl if acpl is not None else 'n/a'}. "
        f"Instructive mistakes: {n_mistakes} (worst: {worst_severity or 'none'}). "
        f"Good highlighted moments: {n_highlights}. "
        "Do not invent specific moves or positions you were not given."
    )
    payload = {
        "model": os.environ.get("HERMES_MODEL", "hermes"),
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are a concise, honest chess coach."},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
            resp = await client.post(f"{_base_url()}/chat/completions", json=payload, headers=headers)
        if resp.status_code < 400:
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            if text:
                return text, "hermes"
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return fallback, "local"


__all__ = [
    "build_router", "CoachStatus", "ChatMessage", "ChatRequest",
    "generate_review_summary",
]
