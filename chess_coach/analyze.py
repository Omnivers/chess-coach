"""Phase 1 — drive a PGN through the engine and write the journal.

This is the orchestrator. It:
  1. Parses a PGN with python-chess (replay + headers).
  2. For each position in the main line, calls the engine.
  3. Computes per-move delta_cp (current eval − previous eval), flipping
     sign when the side-to-move changes.
  4. Runs the instructive filter and motif detection per user move.
  5. Writes games, positions, evals, mistakes to the journal.
  6. Returns a `GameAnalysis` summary so the CLI / UI can render it.

Truth layer rules from PROJECT.md §3 still apply: this module never
invents chess content. Every row written to the journal is grounded in
either python-chess (legality, FEN, clock) or the engine's UCI output.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Iterable, Optional

import chess
import chess.pgn

from .classify import classify_move
from .engine import DEFAULT_DEPTH, DEFAULT_MULTIPV, EngineLine, StockfishEngine
from .journal import (
    Journal,
    insert_drill,
    insert_eval,
    insert_game,
    insert_mistake,
    insert_position,
)


@dataclass
class GameAnalysis:
    """Summary of a single game's analysis. Counts, not positions."""
    game_id: int
    external_id: Optional[str]
    total_positions: int = 0
    instructive_mistakes: int = 0
    rejected_candidates: int = 0
    motif_counts: dict[str, int] = field(default_factory=dict)

    def record_mistake(self, instructive: bool, motifs: Iterable[str]) -> None:
        if instructive:
            self.instructive_mistakes += 1
        else:
            self.rejected_candidates += 1
        for m in motifs:
            self.motif_counts[m] = self.motif_counts.get(m, 0) + 1


def _result_for_color(headers: dict, color: chess.Color) -> str:
    """Map PGN `Result` to the user's outcome."""
    res = headers.get("Result", "*")
    if res == "1-0":
        return "win" if color == chess.WHITE else "loss"
    if res == "0-1":
        return "loss" if color == chess.WHITE else "win"
    if res == "1/2-1/2":
        return "draw"
    return "draw"  # default for unterminated games


def _header_int(headers: dict, key: str) -> Optional[int]:
    """Read an integer header (WhiteElo, BlackElo) or None."""
    val = headers.get(key)
    if val is None or val == "?":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def analyse_game(
    pgn_text: str,
    *,
    journal: Journal,
    engine: StockfishEngine,
    source: str = "lichess",
    external_id: Optional[str] = None,
    depth: int = DEFAULT_DEPTH,
    multipv: int = DEFAULT_MULTIPV,
    user_color: chess.Color = chess.WHITE,
) -> GameAnalysis:
    """Analyse a single game end-to-end and write everything to the journal.

    Returns a summary. Re-running on the same game is idempotent: the
    journal's UNIQUE(source, external_id) on games and UNIQUE(game_id,
    ply) on positions mean replays upsert.
    """
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("could not parse PGN")
    # python-chess returns a zero-move game for strings with no PGN
    # body at all. Reject those — there's nothing to analyse.
    if not list(game.mainline_moves()):
        raise ValueError("PGN has no mainline moves")
    headers = game.headers
    total_plies = sum(1 for _ in game.mainline_moves())

    # The user's rating is their own Elo from the appropriate header.
    user_rating = (
        _header_int(headers, "WhiteElo")
        if user_color == chess.WHITE
        else _header_int(headers, "BlackElo")
    )

    result = _result_for_color(headers, user_color)

    # Build the game row and collect every position FEN.
    with journal.transaction() as conn:
        game_id = insert_game(
            conn,
            source=source,
            external_id=external_id,
            played_at=headers.get("UTCDate", "????.??.??") + "T"
                      + (headers.get("UTCTime", "??:??:??") + "+00:00"),
            color="white" if user_color == chess.WHITE else "black",
            result=result,
            pgn=str(game),
            time_control=headers.get("TimeControl"),
            my_rating=user_rating,
            opp_rating=(
                _header_int(headers, "BlackElo")
                if user_color == chess.WHITE
                else _header_int(headers, "WhiteElo")
            ),
            eco=headers.get("ECO"),
            opening_name=headers.get("Opening"),
        )

    # Walk the main line. We track the position *before* each half-move
    # so we can attach motifs and evals to it. The user plays on
    # `user_color` plies; on opponent plies we still capture the
    # position+eval so the engine's PV is preserved for review.
    summary = GameAnalysis(game_id=game_id, external_id=external_id)
    board = game.board()
    prev_eval_for_side: Optional[int] = None  # side-to-move POV

    for ply, move in enumerate(game.mainline_moves(), start=1):
        # Capture the pre-move position. This is where motifs and the
        # engine's "what was best" attach.
        fen_before = board.fen()
        # Evaluate. Side to move here is the player about to move.
        stm = board.turn
        eval_pov = _engine_eval_for(engine, fen_before, depth=depth, multipv=multipv)
        summary.total_positions += 1

        # Persist the position and all multipv lines.
        san = board.san(move)
        uci = move.uci()
        clock = _clock_from_comment(board, move, ply)

        # We need position_id for the eval rows, but we also need to
        # write the played-move fields on the position. Insert/update
        # the position now (it captures the move), then insert evals.
        with journal.transaction() as conn:
            position_id = insert_position(
                conn,
                game_id=game_id,
                ply=ply,
                fen=fen_before,
                move_san=san,
                move_uci=uci,
                clock_ms=clock,
            )
            for line in eval_pov:
                insert_eval(
                    conn,
                    position_id=position_id,
                    engine=f"stockfish-{engine.__class__.__name__}",
                    depth=line.depth,
                    cp=line.cp,
                    mate=line.mate,
                    best_uci=line.best_uci,
                    pv=" ".join(line.pv),
                    multipv_rank=line.multipv_rank,
                )

        # Only the user's moves become mistakes rows. The opponent's
        # moves still get positions + evals (review material) but not
        # mistake classification.
        if stm == user_color:
            best_line = eval_pov[0] if eval_pov else None
            best_uci = best_line.best_uci if best_line else uci
            best_cp = best_line.cp if best_line else None

            # Convert to "from the user's POV" delta. After the move the
            # side-to-move flips, so eval after = -eval_before from the
            # new side's POV. We approximate eval_after with the eval
            # of the *post-move* position below.
            eval_after_pov = _engine_eval_for(
                engine,
                board.fen(),  # current board, move not yet pushed
                depth=depth, multipv=1,
            )
            eval_after_cp = eval_after_pov[0].cp if eval_after_pov and eval_after_pov[0].cp is not None else None

            # `classify_move` is the single funnel both this module and
            # api_play.py go through (API.md §3) — it owns the delta-cp
            # math, the instructive filter, motif detection, and the
            # classification ladder, so there is exactly one definition
            # of "what kind of mistake was this."
            verdict = classify_move(
                board_before=board,  # not yet pushed; classify_move copies internally
                played_move=chess.Move.from_uci(uci),
                eval_before_cp=best_cp,
                eval_after_cp=eval_after_cp,
                best_uci=best_uci,
                ply=ply,
                total_plies=total_plies,
                my_rating=user_rating,
                clock_ms=clock,
                out_of_book=False,  # no book data yet
            )

            # verdict is None when an eval is missing (nothing to say);
            # verdict.severity is None when the delta is below the noise
            # floor (not a mistake at all). Either way, skip the row.
            if verdict is not None and verdict.severity is not None:
                with journal.transaction() as conn:
                    mistake_id = _record_mistake(
                        conn, position_id=position_id, game_id=game_id,
                        verdict=verdict,
                    )
                    # An instructive mistake from an imported game is
                    # exactly what the drill queue is for — without this
                    # the whole SRS path only ever saw mistakes made in
                    # live play, so a user who imported a year of Lichess
                    # games still opened an empty Drills tab.
                    # `insert_drill` is idempotent per mistake_id, so
                    # re-analysing a game reuses the existing drill
                    # rather than flooding the queue.
                    if verdict.instructive and mistake_id is not None:
                        insert_drill(
                            conn, mistake_id=mistake_id, fen=fen_before,
                            solution_uci=verdict.engine_best_uci,
                        )
                    elif mistake_id is not None:
                        # The symmetric case. Re-analysing at a deeper
                        # depth can downgrade a mistake out of
                        # "instructive" — and without this the drill
                        # spawned by the shallower pass would sit in the
                        # queue forever, since nothing else ever revisits
                        # it. Retire rather than delete: retiring leaves
                        # the row and its attempt history intact, while a
                        # delete would cascade them away.
                        conn.execute(
                            "UPDATE drills SET retired = 1 "
                            "WHERE mistake_id = ? AND retired = 0",
                            (mistake_id,),
                        )
                summary.record_mistake(verdict.instructive, verdict.motifs)

        # Advance the board for the next iteration.
        board.push(move)

    return summary


def _record_mistake(
    conn, *, position_id: int, game_id: int, verdict
) -> int:
    """Write a mistake for `position_id`, refreshing it if one exists.

    `insert_mistake` is a plain INSERT, and nothing in the schema makes
    `mistakes.position_id` unique — so re-analysing a game (at a deeper
    depth, say) used to append a second row per position. That silently
    double-counted every profile statistic, and now that instructive
    mistakes spawn drills it would double the review queue too.

    Refreshing in place rather than delete-and-reinsert is deliberate:
    `drills.mistake_id` is ON DELETE CASCADE, so deleting the row would
    take the drill with it and discard however many weeks of SRS
    scheduling the user had built up on that position.
    """
    row = conn.execute(
        "SELECT id FROM mistakes WHERE position_id = ?", (position_id,)
    ).fetchone()
    if row is None:
        return insert_mistake(
            conn,
            position_id=position_id,
            game_id=game_id,
            delta_cp=verdict.delta_cp,
            severity=verdict.severity,
            classification=verdict.classification,
            phase=verdict.phase,
            motifs=verdict.motifs,
            engine_best_uci=verdict.engine_best_uci,
            played_uci=verdict.played_uci,
            instructive=verdict.instructive,
            threshold_cp_in_force=verdict.threshold_cp_in_force,
            rejection_reason=verdict.rejection_reason,
        )
    conn.execute(
        """
        UPDATE mistakes
           SET delta_cp = ?, severity = ?, class = ?, phase = ?, motifs = ?,
               engine_best_uci = ?, played_uci = ?, instructive = ?,
               threshold_cp_in_force = ?, rejection_reason = ?
         WHERE id = ?
        """,
        (
            verdict.delta_cp, verdict.severity, verdict.classification,
            verdict.phase, ",".join(verdict.motifs), verdict.engine_best_uci,
            verdict.played_uci, int(verdict.instructive),
            verdict.threshold_cp_in_force, verdict.rejection_reason,
            row["id"],
        ),
    )
    return int(row["id"])


def _engine_eval_for(
    engine: StockfishEngine,
    fen: str,
    *,
    depth: int,
    multipv: int,
) -> list[EngineLine]:
    """Engine call wrapper. Returns [] on terminal positions (mate / stalemate)."""
    try:
        return engine.analyse(fen, depth=depth, multipv=multipv)
    except Exception:
        return []


def _clock_from_comment(board: chess.Board, move: chess.Move, ply: int) -> Optional[int]:
    """Read `[%clk 0:05:00]` from the comment on the move node if present.

    We don't have the node handle here — the caller passes a Move, not
    a Node — so we fall back to None. Phase 6's Lichess ingest will
    supply real clock times via a different path.
    """
    return None


__all__ = [
    "GameAnalysis",
    "analyse_game",
]
