"""Phase 3 — review endpoints. Read-only over the journal."""
from __future__ import annotations

import io
import sqlite3
from typing import Optional

import chess
import chess.pgn
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .api_coach import generate_review_summary
from .journal import Journal
from .review_build import (
    ReviewDetail,
    ReviewHighlight,
    ReviewMistake,
    ReviewMove,
    build_review,
)

# Ranks worst-first so `max()` over a game's mistakes picks the one to
# lead the coach summary with. Matches `journal.VALID_SEVERITY`'s meaning.
_SEVERITY_RANK = {"inaccuracy": 1, "mistake": 2, "blunder": 3}

# NAG (Numeric Annotation Glyph) codes for the annotated PGN export.
_SEVERITY_NAG = {
    "blunder": chess.pgn.NAG_BLUNDER,
    "mistake": chess.pgn.NAG_MISTAKE,
    "inaccuracy": chess.pgn.NAG_DUBIOUS_MOVE,
}


class ReviewSummary(BaseModel):
    text: str
    source: str


class GameSummary(BaseModel):
    id: int
    external_id: Optional[str]
    source: str
    played_at: str
    color: str
    result: str
    time_control: Optional[str]
    my_rating: Optional[int]
    opp_rating: Optional[int]
    eco: Optional[str]
    opening_name: Optional[str]
    instructive_mistakes: int
    rejected_candidates: int


class EvalLine(BaseModel):
    multipv_rank: int
    depth: int
    cp: Optional[int]
    mate: Optional[int]
    best_uci: str
    pv: list[str]


class PositionDetail(BaseModel):
    ply: int
    fen: str
    move_san: Optional[str]
    move_uci: Optional[str]
    clock_ms: Optional[int]
    evals: list[EvalLine]


class GameDetail(BaseModel):
    id: int
    external_id: Optional[str]
    source: str
    played_at: str
    color: str
    result: str
    my_rating: Optional[int]
    pgn: str
    total_plies: int
    positions: list[PositionDetail]


class MistakeSummary(BaseModel):
    id: int
    game_id: int
    external_id: Optional[str]
    ply: int
    delta_cp: int
    severity: str
    classification: str
    phase: str
    motifs: list[str]
    engine_best_uci: str
    played_uci: str
    instructive: bool
    threshold_cp_in_force: int
    rejection_reason: Optional[str]


def build_router(journal: Journal) -> APIRouter:
    r = APIRouter()

    @r.get("/games", response_model=list[GameSummary])
    def list_games(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[GameSummary]:
        with journal.read() as conn:
            rows = conn.execute(
                """
                SELECT
                    g.id, g.external_id, g.source, g.played_at, g.color, g.result,
                    g.time_control, g.my_rating, g.opp_rating, g.eco, g.opening_name,
                    COALESCE(SUM(CASE WHEN m.instructive = 1 THEN 1 ELSE 0 END), 0) AS instructive,
                    COALESCE(SUM(CASE WHEN m.instructive = 0 THEN 1 ELSE 0 END), 0) AS rejected
                FROM games g
                LEFT JOIN mistakes m ON m.game_id = g.id
                GROUP BY g.id
                ORDER BY g.played_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [
            GameSummary(
                id=r["id"], external_id=r["external_id"], source=r["source"],
                played_at=r["played_at"], color=r["color"], result=r["result"],
                time_control=r["time_control"], my_rating=r["my_rating"],
                opp_rating=r["opp_rating"], eco=r["eco"], opening_name=r["opening_name"],
                instructive_mistakes=r["instructive"], rejected_candidates=r["rejected"],
            )
            for r in rows
        ]

    @r.get("/games/{external_id}", response_model=GameDetail)
    def get_game(external_id: str) -> GameDetail:
        with journal.read() as conn:
            game_row = conn.execute(
                """
                SELECT id, external_id, source, played_at, color, result,
                       my_rating, pgn
                FROM games WHERE external_id = ?
                """,
                (external_id,),
            ).fetchone()
            if game_row is None:
                raise HTTPException(status_code=404, detail="game not found")
            positions = conn.execute(
                "SELECT id, ply, fen, move_san, move_uci, clock_ms "
                "FROM positions WHERE game_id = ? ORDER BY ply ASC",
                (game_row["id"],),
            ).fetchall()
            eval_rows = conn.execute(
                "SELECT position_id, multipv_rank, depth, cp, mate, best_uci, pv "
                "FROM evals WHERE position_id IN (SELECT id FROM positions WHERE game_id = ?) "
                "ORDER BY position_id, multipv_rank",
                (game_row["id"],),
            ).fetchall()
        evals_by_pos: dict[int, list[EvalLine]] = {}
        for er in eval_rows:
            evals_by_pos.setdefault(er["position_id"], []).append(EvalLine(
                multipv_rank=er["multipv_rank"], depth=er["depth"],
                cp=er["cp"], mate=er["mate"], best_uci=er["best_uci"],
                pv=(er["pv"] or "").split() if er["pv"] else [],
            ))
        positions_out = [
            PositionDetail(
                ply=p["ply"], fen=p["fen"],
                move_san=p["move_san"], move_uci=p["move_uci"],
                clock_ms=p["clock_ms"],
                evals=evals_by_pos.get(p["id"], []),
            )
            for p in positions
        ]
        return GameDetail(
            id=game_row["id"], external_id=game_row["external_id"],
            source=game_row["source"], played_at=game_row["played_at"],
            color=game_row["color"], result=game_row["result"],
            my_rating=game_row["my_rating"], pgn=game_row["pgn"],
            total_plies=len(positions), positions=positions_out,
        )

    @r.get("/mistakes", response_model=list[MistakeSummary])
    def list_mistakes(
        instructive_only: bool = Query(False),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[MistakeSummary]:
        with journal.read() as conn:
            rows = conn.execute(
                "SELECT m.*, g.external_id FROM mistakes m "
                "LEFT JOIN games g ON g.id = m.game_id "
                + ("WHERE m.instructive = 1 " if instructive_only else "")
                + "ORDER BY m.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[MistakeSummary] = []
        for r in rows:
            motifs = [m for m in (r["motifs"] or "").split(",") if m]
            out.append(MistakeSummary(
                id=r["id"], game_id=r["game_id"], external_id=r["external_id"],
                ply=r["position_id"], delta_cp=r["delta_cp"], severity=r["severity"],
                classification=r["class"], phase=r["phase"], motifs=motifs,
                engine_best_uci=r["engine_best_uci"], played_uci=r["played_uci"],
                instructive=bool(r["instructive"]),
                threshold_cp_in_force=r["threshold_cp_in_force"],
                rejection_reason=r["rejection_reason"],
            ))
        return out

    @r.get("/review/{external_id}", response_model=ReviewDetail)
    def get_review(external_id: str) -> ReviewDetail:
        with journal.read() as conn:
            review = build_review(conn, external_id=external_id)
        if review is None:
            raise HTTPException(status_code=404, detail="game not found")
        return review

    @r.get("/review/{external_id}/summary", response_model=ReviewSummary)
    async def get_review_summary(external_id: str) -> ReviewSummary:
        with journal.read() as conn:
            review = build_review(conn, external_id=external_id)
        if review is None:
            raise HTTPException(status_code=404, detail="game not found")
        if review.accuracy is None:
            # Never fabricate a 0.0 accuracy to feed the coach — an
            # unanalysed game has nothing to summarise.
            return ReviewSummary(
                text=(
                    "This game hasn't been analysed yet — there's no evaluation "
                    "data to summarise. Run the analysis and come back."
                ),
                source="local",
            )
        worst = (
            max(review.mistakes, key=lambda m: _SEVERITY_RANK.get(m.severity, 0)).severity
            if review.mistakes else None
        )
        text, source = await generate_review_summary(
            accuracy=review.accuracy, acpl=review.acpl,
            n_mistakes=len(review.mistakes), n_highlights=len(review.highlights),
            worst_severity=worst,
        )
        return ReviewSummary(text=text, source=source)

    @r.get("/review/{external_id}/pgn", response_class=PlainTextResponse)
    def get_review_pgn(external_id: str) -> PlainTextResponse:
        with journal.read() as conn:
            game_row = conn.execute(
                "SELECT id, pgn FROM games WHERE external_id = ?", (external_id,)
            ).fetchone()
            if game_row is None:
                raise HTTPException(status_code=404, detail="game not found")
            mistake_rows = conn.execute(
                "SELECT p.ply AS ply, m.severity, m.delta_cp, m.engine_best_uci, m.explanation "
                "FROM mistakes m JOIN positions p ON p.id = m.position_id "
                "WHERE m.game_id = ? AND m.instructive = 1",
                (game_row["id"],),
            ).fetchall()
            highlight_rows = conn.execute(
                "SELECT ply, kind FROM highlights WHERE game_id = ?",
                (game_row["id"],),
            ).fetchall()

        annotated = _annotate_pgn(game_row["pgn"], mistake_rows, highlight_rows)
        return PlainTextResponse(
            annotated,
            media_type="application/x-chess-pgn",
            headers={"Content-Disposition": f'attachment; filename="{external_id}.pgn"'},
        )

    return r


def _annotate_pgn(
    pgn_text: str,
    mistake_rows: list[sqlite3.Row],
    highlight_rows: list[sqlite3.Row],
) -> str:
    """Add NAGs + comments for instructive mistakes and highlights.

    Falls back to the raw stored PGN, unchanged, if it won't parse — the
    download link must never break a game's review over a bad PGN row.
    """
    parsed = chess.pgn.read_game(io.StringIO(pgn_text))
    if parsed is None:
        return pgn_text

    mistake_by_ply = {row["ply"]: row for row in mistake_rows}
    highlight_by_ply = {row["ply"]: row for row in highlight_rows}

    for ply, node in enumerate(parsed.mainline(), start=1):
        comments: list[str] = [node.comment] if node.comment else []
        mistake = mistake_by_ply.get(ply)
        if mistake is not None:
            nag = _SEVERITY_NAG.get(mistake["severity"])
            if nag is not None:
                node.nags.add(nag)
            text = (
                f"{mistake['severity']}: {mistake['delta_cp']}cp. "
                f"Engine preferred {mistake['engine_best_uci']}."
            )
            if mistake["explanation"]:
                text += f" {mistake['explanation']}"
            comments.append(text)
        highlight = highlight_by_ply.get(ply)
        if highlight is not None:
            node.nags.add(chess.pgn.NAG_GOOD_MOVE)
            comments.append(highlight["kind"].replace("_", " "))
        node.comment = " ".join(comments)

    return str(parsed)


__all__ = [
    "build_router",
    "GameSummary", "GameDetail", "EvalLine", "PositionDetail", "MistakeSummary",
    "ReviewDetail", "ReviewMove", "ReviewMistake", "ReviewHighlight", "ReviewSummary",
]
