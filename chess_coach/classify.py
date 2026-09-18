"""Phase 2 — the shared classification funnel (ROADMAP §1.1, API.md §3).

Before this module existed, `analyze.py` and `api_play.py` each built a
`mistakes` row by hand, and the hand-rolled version in `analyze.py` had a
placeholder that always wrote `classification="tactical"`. Two call sites
computing "was this a mistake, and what kind" independently is exactly how
they drift. `classify_move` is now the only place that answers that
question — both callers pass it the same inputs and get the same verdict.

Pure function: no DB, no engine calls. The caller has already gotten evals
from Stockfish; this module only reasons about numbers and the board.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import chess

from .analysis import (
    detect_motifs_before_move,
    evaluate_filter,
    severity_from_delta,
)

# Below this remaining clock, a mistake is presumed time-pressure-driven
# rather than a calculation error — the classification ladder checks this
# first, ahead of everything else, because a blunder made with three
# seconds left teaches a different lesson than the same blunder made with
# time to think.
TIME_PRESSURE_MS = 30_000


@dataclass(frozen=True)
class MoveVerdict:
    """Everything downstream needs to decide whether/how to record a move."""

    is_mistake: bool
    delta_cp: int
    severity: Optional[str]          # inaccuracy|mistake|blunder, None if below noise
    classification: str              # tactical|positional|opening|endgame|time
    phase: str                       # opening|middlegame|endgame
    motifs: list[str]
    instructive: bool
    threshold_cp_in_force: int
    rejection_reason: Optional[str]
    engine_best_uci: str
    played_uci: str


def _phase_for_ply(ply: int, total_plies: int) -> str:
    """Roughly partition a game into opening / middlegame / endgame.

    These are heuristics, not engines. They are good enough to give the
    journal a `phase` column that means something without claiming
    anything precise about where the middlegame ends. The user can
    later fit a better partition from `skill_snapshots`.
    """
    if ply <= 16:
        return "opening"
    # Last 1/4 of the game (by ply) is the endgame.
    if ply >= total_plies * 3 // 4:
        return "endgame"
    return "middlegame"


def classify_move(
    *,
    board_before: chess.Board,
    played_move: chess.Move,
    eval_before_cp: Optional[int],
    eval_after_cp: Optional[int],
    best_uci: Optional[str],
    ply: int,
    total_plies: int,
    my_rating: Optional[int],
    clock_ms: Optional[int] = None,
    out_of_book: bool = False,
) -> Optional[MoveVerdict]:
    """Classify one played move against the engine's evaluation.

    Rules, in order (API.md §3):
      1. `None` when either eval is missing — there is nothing to say.
      2. `delta_cp = (-eval_after_cp) - eval_before_cp`, both to user POV.
      3. `severity_from_delta`; below the 50cp noise floor it raises, and
         we return a verdict with `is_mistake=False, severity=None` rather
         than propagating the exception — but every other field is still
         computed normally, because a highlight (e.g. `best_under_pressure`)
         can still fire on a technically-non-mistake move.
      4-7. phase, motifs, classification, and the instructive filter are
         computed unconditionally.
    """
    if eval_before_cp is None or eval_after_cp is None:
        return None

    delta_cp = (-eval_after_cp) - eval_before_cp
    resolved_best_uci = best_uci or played_move.uci()

    try:
        severity = severity_from_delta(delta_cp)
        is_mistake = True
    except ValueError:
        severity = None
        is_mistake = False

    phase = _phase_for_ply(ply, total_plies)
    motifs = detect_motifs_before_move(
        board_before.copy(), played_move, resolved_best_uci
    )
    classification = _classify(
        phase=phase, motifs=motifs, clock_ms=clock_ms, out_of_book=out_of_book
    )

    verdict = evaluate_filter(
        delta_cp=delta_cp,
        eval_before_cp=eval_before_cp,
        my_rating=my_rating,
        best_uci=resolved_best_uci,
        board_for_refutation=board_before.copy(),
    )

    return MoveVerdict(
        is_mistake=is_mistake,
        delta_cp=delta_cp,
        severity=severity,
        classification=classification,
        phase=phase,
        motifs=motifs,
        instructive=verdict.instructive,
        threshold_cp_in_force=verdict.threshold_cp_in_force,
        rejection_reason=(
            verdict.rejection_reason.value if verdict.rejection_reason else None
        ),
        engine_best_uci=resolved_best_uci,
        played_uci=played_move.uci(),
    )


def _classify(
    *, phase: str, motifs: list[str], clock_ms: Optional[int], out_of_book: bool
) -> str:
    """First match wins. Must only ever return a member of `journal.VALID_CLASS`."""
    if clock_ms is not None and clock_ms < TIME_PRESSURE_MS:
        return "time"
    if phase == "opening" and out_of_book:
        return "opening"
    if phase == "endgame":
        return "endgame"
    if motifs:
        return "tactical"
    return "positional"


__all__ = [
    "TIME_PRESSURE_MS",
    "MoveVerdict",
    "classify_move",
    "_phase_for_ply",
]
