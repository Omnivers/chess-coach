"""Phase 2 — the positive class (API.md §4).

`mistakes` rows answer "what went wrong." Highlights answer the opposite
question: "what went right, and is it worth showing the user." A game
full of only mistakes is a demoralizing review; a game that also surfaces
the only move found under time pressure, or a tactic that was calculated
correctly, is a coaching tool.

Pure function: no DB, no engine calls. The caller already has the
multipv lines for the position (`lines_before`) and the pre-computed
`delta_cp` for the played move — this module only reasons about those
numbers and the board.
"""
from __future__ import annotations

from typing import Optional

import chess

from .analysis import detect_motifs_after_move
from .engine import EngineLine

# A move is the "only move" when it's both engine-best and clearly ahead
# of the second-best alternative by this many centipawns — anything less
# and a human could plausibly have found the runner-up too.
ONLY_MOVE_MARGIN_CP = 150

# A tactic is "found" when it swings the position by at least this much
# in the user's favour and a concrete motif appears after the move.
FOUND_TACTIC_GAIN_CP = 150

# "Resisted" fires when the position was already lost-ish but the move
# didn't make it meaningfully worse — a save, not a blunder pile-on.
RESISTED_LOSS_CP = 20
RESISTED_WORSE_CP = -200

# Named per the contract's constant list. Used here (rather than the
# literal 300 that appears in the precedence table's prose) per the
# project's "named constants for meaningful thresholds" rule — see the
# discrepancy note in this module's final report.
CONVERTED_FLOOR_CP = 200

# Below this remaining clock, playing the engine's best move under
# pressure is itself highlight-worthy, independent of severity.
PRESSURE_MS = 30_000


def _mate_as_cp(cp: Optional[int], mate: Optional[int]) -> int:
    """Comparison-only stand-in for a mate score. Never stored anywhere.

    A mate score has no real centipawn value, but the highlight logic
    needs *some* number to compare against thresholds like "ahead by
    150cp." +/-10_000 is far outside any real evaluation, so it always
    wins/loses those comparisons the way a forced mate should.
    """
    if cp is not None:
        return cp
    if mate is not None:
        return 10_000 if mate > 0 else -10_000
    return 0


def detect_highlight(
    *,
    board_before: chess.Board,
    played_move: chess.Move,
    lines_before: list[EngineLine],
    delta_cp: int,
    eval_before_cp: Optional[int],
    phase: str,
    clock_ms: Optional[int],
) -> Optional[tuple[str, Optional[int]]]:
    """Classify a played move into a positive-class highlight, if any.

    Returns `(kind, delta_to_2nd)` or `None`. First match wins, in the
    order given by API.md §4: only_move, found_tactic, resisted,
    converted, best_under_pressure.
    """
    played_uci = played_move.uci()
    best_uci = lines_before[0].best_uci if lines_before else None

    if (
        best_uci is not None
        and played_uci == best_uci
        and len(lines_before) >= 2
    ):
        cp0 = _mate_as_cp(lines_before[0].cp, lines_before[0].mate)
        cp1 = _mate_as_cp(lines_before[1].cp, lines_before[1].mate)
        delta_to_2nd = cp0 - cp1
        if delta_to_2nd >= ONLY_MOVE_MARGIN_CP:
            return ("only_move", delta_to_2nd)

    if delta_cp >= FOUND_TACTIC_GAIN_CP:
        board_after = board_before.copy()
        board_after.push(played_move)
        if detect_motifs_after_move(board_after, played_move):
            return ("found_tactic", None)

    if (
        eval_before_cp is not None
        and eval_before_cp <= RESISTED_WORSE_CP
        and delta_cp >= -RESISTED_LOSS_CP
    ):
        return ("resisted", None)

    if (
        phase == "endgame"
        and eval_before_cp is not None
        and eval_before_cp >= CONVERTED_FLOOR_CP
        and delta_cp >= -RESISTED_LOSS_CP
    ):
        return ("converted", None)

    if (
        best_uci is not None
        and played_uci == best_uci
        and clock_ms is not None
        and clock_ms < PRESSURE_MS
    ):
        return ("best_under_pressure", None)

    return None


__all__ = [
    "ONLY_MOVE_MARGIN_CP",
    "FOUND_TACTIC_GAIN_CP",
    "RESISTED_LOSS_CP",
    "RESISTED_WORSE_CP",
    "CONVERTED_FLOOR_CP",
    "PRESSURE_MS",
    "detect_highlight",
]
