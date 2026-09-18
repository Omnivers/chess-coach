"""Phase 6 — assembles one game's `/review/{external_id}` payload.

Pulled out of `api_review.py` so that module stays a thin HTTP layer.
This is the one place that turns `positions` + `evals` + `mistakes` +
`highlights` rows into the shape the review page and the eval scrubber
actually render — see `web/js/review.js` and `web/js/scrubber.js` for
the consumers this must satisfy.

Response models live here rather than in `api_review.py` because they
are defined by the query shapes below; `api_review.py` imports and
re-exports them so callers never need to know this module exists.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import chess
from pydantic import BaseModel

from .journal import list_highlights
from .review import side_to_move_at, summarise, user_move_evals


class ReviewMove(BaseModel):
    ply: int  # 0-based index into moves[] — NOT positions.ply
    san: str
    uci: Optional[str]
    fen: str  # position AFTER this move
    eval_cp: Optional[int]  # user POV, after this move
    eval_mate: Optional[int]  # user POV, after this move
    time_ms: Optional[int]  # time the mover spent on this move


class ReviewMistake(BaseModel):
    ply: int  # 0-based index into moves[]
    severity: str
    motif: Optional[str]  # first motif, or None
    motifs: list[str]
    classification: str
    phase: str
    delta_cp: int
    best_uci: str
    played_uci: str
    explanation: Optional[str]


class ReviewHighlight(BaseModel):
    ply: int  # 0-based index into moves[]
    kind: str
    note: Optional[str]
    played_uci: str
    delta_to_2nd: Optional[int]


class ReviewDetail(BaseModel):
    external_id: str
    color: str
    result: str
    played_at: str
    eco: Optional[str]
    opening_name: Optional[str]
    accuracy: Optional[float]
    acpl: Optional[int]
    moves: list[ReviewMove]
    mistakes: list[ReviewMistake]
    highlights: list[ReviewHighlight]


def build_review(conn: sqlite3.Connection, *, external_id: str) -> Optional[ReviewDetail]:
    """Assemble the full review for one game, or None if it doesn't exist.

    Read-only: takes an already-open connection so callers (both the
    `/review/{id}` route and the `/summary` route, which needs the same
    aggregates) share one query pass instead of duplicating the SQL.
    """
    game = conn.execute(
        """
        SELECT id, external_id, color, result, played_at, eco, opening_name, pgn
          FROM games WHERE external_id = ?
        """,
        (external_id,),
    ).fetchone()
    if game is None:
        return None

    positions = conn.execute(
        "SELECT id, ply, fen, move_san, move_uci, clock_ms "
        "FROM positions WHERE game_id = ? ORDER BY ply ASC",
        (game["id"],),
    ).fetchall()
    # Only rows with a played move become entries in moves[]; a position
    # row can in principle carry no move (e.g. a game truncated mid-write).
    played = [p for p in positions if p["move_san"] is not None]
    position_by_ply = {p["ply"]: p for p in positions}
    index_by_position_ply = {p["ply"]: i for i, p in enumerate(played)}

    eval_by_position_id = _rank1_evals_by_position(conn, game_id=game["id"])
    moves = [
        _build_move(played, i, position_by_ply, eval_by_position_id, user_color=game["color"])
        for i in range(len(played))
    ]

    mistake_rows = conn.execute(
        """
        SELECT m.*, p.ply AS position_ply
          FROM mistakes m
          JOIN positions p ON p.id = m.position_id
         WHERE m.game_id = ? AND m.instructive = 1
         ORDER BY p.ply ASC
        """,
        (game["id"],),
    ).fetchall()
    mistakes = [
        _build_mistake(row, index_by_position_ply[row["position_ply"]])
        for row in mistake_rows
        if row["position_ply"] in index_by_position_ply
    ]

    highlights = [
        _build_highlight(row, index_by_position_ply[row["ply"]])
        for row in list_highlights(conn, game_id=game["id"])
        if row["ply"] in index_by_position_ply
    ]

    evals = user_move_evals(conn, game_id=game["id"], user_color=game["color"])
    accuracy, acpl = summarise([(e.before_cp, e.after_cp) for e in evals])

    return ReviewDetail(
        external_id=game["external_id"], color=game["color"], result=game["result"],
        played_at=game["played_at"], eco=game["eco"], opening_name=game["opening_name"],
        accuracy=accuracy, acpl=acpl, moves=moves, mistakes=mistakes, highlights=highlights,
    )


def _rank1_evals_by_position(
    conn: sqlite3.Connection, *, game_id: int
) -> dict[int, tuple[Optional[int], Optional[int]]]:
    rows = conn.execute(
        """
        SELECT position_id, cp, mate FROM evals
         WHERE multipv_rank = 1
           AND position_id IN (SELECT id FROM positions WHERE game_id = ?)
        """,
        (game_id,),
    ).fetchall()
    # A position can carry several rank-1 rows (re-analysed at higher
    # depth); iterate in row order and let the last one win.
    out: dict[int, tuple[Optional[int], Optional[int]]] = {}
    for r in rows:
        out[r["position_id"]] = (r["cp"], r["mate"])
    return out


def _fen_after(row: sqlite3.Row) -> str:
    """FEN of the position after `row`'s move, computed when no next
    position row is available to read it from directly."""
    try:
        board = chess.Board(row["fen"])
        board.push_uci(row["move_uci"])
        return board.fen()
    except (ValueError, AssertionError):
        return row["fen"]


def _build_move(
    played: list[sqlite3.Row],
    i: int,
    position_by_ply: dict[int, sqlite3.Row],
    eval_by_position_id: dict[int, tuple[Optional[int], Optional[int]]],
    *,
    user_color: str,
) -> ReviewMove:
    row = played[i]
    next_row = position_by_ply.get(row["ply"] + 1)

    fen = next_row["fen"] if next_row is not None else _fen_after(row)

    eval_cp: Optional[int] = None
    eval_mate: Optional[int] = None
    if next_row is not None:
        pair = eval_by_position_id.get(next_row["id"])
        if pair is not None:
            cp, mate = pair
            # The journal stores this eval from the *next* position's
            # side-to-move POV; flip both fields when that isn't the
            # user, so eval_cp/eval_mate here are always user POV.
            flip = side_to_move_at(next_row["ply"]) != user_color
            eval_cp = (-cp if flip else cp) if cp is not None else None
            eval_mate = (-mate if flip else mate) if mate is not None else None

    time_ms: Optional[int] = None
    if i >= 2 and played[i - 2]["clock_ms"] is not None and row["clock_ms"] is not None:
        # clock_ms is the clock REMAINING after a move; the same
        # player's previous move sits two plies back (their opponent's
        # move is in between). A negative delta means the increment
        # added back more than was spent, and we have no increment
        # value here to net it out — decline to guess rather than
        # report a wrong number.
        delta = played[i - 2]["clock_ms"] - row["clock_ms"]
        time_ms = delta if delta >= 0 else None

    return ReviewMove(
        ply=i, san=row["move_san"], uci=row["move_uci"], fen=fen,
        eval_cp=eval_cp, eval_mate=eval_mate, time_ms=time_ms,
    )


def _build_mistake(row: sqlite3.Row, ply: int) -> ReviewMistake:
    motifs = [m for m in (row["motifs"] or "").split(",") if m]
    return ReviewMistake(
        ply=ply, severity=row["severity"], motif=motifs[0] if motifs else None,
        motifs=motifs, classification=row["class"], phase=row["phase"],
        delta_cp=row["delta_cp"], best_uci=row["engine_best_uci"],
        played_uci=row["played_uci"], explanation=row["explanation"],
    )


def _build_highlight(row: sqlite3.Row, ply: int) -> ReviewHighlight:
    return ReviewHighlight(
        ply=ply, kind=row["kind"], note=row["note"],
        played_uci=row["played_uci"], delta_to_2nd=row["delta_to_2nd"],
    )


__all__ = [
    "ReviewMove", "ReviewMistake", "ReviewHighlight", "ReviewDetail",
    "build_review",
]
