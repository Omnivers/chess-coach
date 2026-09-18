"""Phase 5 — engine-backed opponent for live play.

A `LiveGame` is an in-memory chess game with Stockfish as one side and
the human as the other. The engine is held in a closure by the API
factory; this module just exposes the LiveGame dataclass and the pure
helper functions that don't need the engine.
"""
from __future__ import annotations

import chess
import chess.pgn
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LiveGame:
    game_id: str
    board: chess.Board
    user_color: chess.Color  # the human plays this color
    engine_color: chess.Color
    db_id: int               # primary key in `games` table
    started_at: float
    engine_elo: int = 1500
    user_rating: Optional[int] = None
    pgn_so_far: list[str] = field(default_factory=list)
    terminated: bool = False
    result: Optional[str] = None  # "win"/"loss"/"draw" from user's POV
    last_pos_id: int = 0
    # Server-authoritative clock. None/None/0 means "unlimited" — no
    # clock is tracked and the API always reports `clock: null`.
    white_ms: Optional[int] = None
    black_ms: Optional[int] = None
    increment_ms: int = 0
    # Hint ladder (ROADMAP §2.1): 6 credits/game, never replenished.
    hint_credits: int = 6
    guard_fired: bool = False


TIME_CONTROLS: dict[str, Optional[tuple[int, int]]] = {
    "15+10": (15 * 60_000, 10_000),
    "10+5": (10 * 60_000, 5_000),
    "5+3": (5 * 60_000, 3_000),
    "3+2": (3 * 60_000, 2_000),
    "unlimited": None,
}


def parse_time_control(time_control: str) -> Optional[tuple[int, int]]:
    """Return (base_ms, increment_ms), or None for "unlimited".

    Raises ValueError for anything not in the supported set — never guess
    at a clock the client didn't ask for.
    """
    if time_control not in TIME_CONTROLS:
        raise ValueError(f"unsupported time_control: {time_control}")
    return TIME_CONTROLS[time_control]


def ply_of(board: chess.Board) -> int:
    """The position where the Nth half-move is about to be played.

    Convention (matches `analyze.py` and the `positions.ply` column):
    1 = starting position (white to move), 2 = after 1.e4 (black to move),
    3 = after 1...e5 (white to move), 4 = after 2.Nf3, etc.
    """
    return board.fullmove_number * 2 - (1 if board.turn == chess.WHITE else 0)


def check_termination(lg: LiveGame) -> tuple[bool, Optional[str]]:
    """Return (terminated, result_from_user_pov) for the current position."""
    b = lg.board
    over = (
        b.is_checkmate()
        or b.is_stalemate()
        or b.is_insufficient_material()
        or b.is_seventyfive_moves()
        or b.is_fivefold_repetition()
        or b.can_claim_threefold_repetition()
    )
    if not over:
        return False, None
    if b.is_checkmate():
        return True, "loss" if b.turn == lg.user_color else "win"
    return True, "draw"


def build_pgn(lg: LiveGame) -> str:
    """Reconstruct a PGN from the move history. Headers are best-effort."""
    game = chess.pgn.Game()
    game.headers["Event"] = "Casual Game"
    game.headers["Site"] = "chess-coach-local"
    game.headers["Date"] = time.strftime("%Y.%m.%d", time.gmtime(lg.started_at))
    game.headers["Round"] = "?"
    if lg.user_color == chess.WHITE:
        game.headers["White"] = "User"
        game.headers["Black"] = "Stockfish"
    else:
        game.headers["White"] = "Stockfish"
        game.headers["Black"] = "User"
    game.headers["Result"] = {"win": "1-0", "loss": "0-1", "draw": "1/2-1/2"}.get(
        lg.result or "", "*"
    )
    node = game
    board = chess.Board()
    for san in lg.pgn_so_far:
        try:
            move = board.parse_san(san)
        except (chess.InvalidMoveError, ValueError):
            break
        node = node.add_variation(move)
        board.push(move)
    return str(game)


__all__ = [
    "LiveGame", "ply_of", "check_termination", "build_pgn",
    "TIME_CONTROLS", "parse_time_control",
]
