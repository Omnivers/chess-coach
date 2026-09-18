"""Phase 6 — the review math shared by the review page and the profile.

Pure (and near-pure) helpers over the journal's `positions` + `evals`
rows. Two callers need exactly the same numbers and must not disagree
about them:

  * `api_review.py`  — accuracy and ACPL for one game's review page
  * `api_session.py` — ACPL overall and per phase across every game

so the definitions live here once rather than being re-derived on each
side. Nothing in this module talks to an engine, an LLM, or the network:
it reads rows that `analyze.py` already wrote.

Sign conventions match `classify.py` exactly — evals come out of the
journal from the *side to move's* point of view, and everything below is
converted to the **user's** point of view (positive = the user stands
better) before any arithmetic happens.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

from .classify import _phase_for_ply as phase_for_ply  # public via classify.__all__

# A mate score has no centipawn value. `highlights.py` uses +/-10_000 for
# threshold comparisons, but that saturates the win-percent curve below
# and would let one forced mate dominate an average. +/-1000cp is already
# ~97% win probability — decisive, without swamping the mean.
MATE_CP = 1000

# No single move counts for more than a queen's worth of loss. Without
# this, one position where the engine found mate turns a whole game's
# ACPL into noise.
MAX_CPL = 1000


@dataclass(frozen=True)
class MoveEval:
    """One of the user's moves, evaluated before and after, user POV."""

    ply: int        # positions.ply (1-based: ply 1 is the game's first move)
    before_cp: int  # eval of the position the user was about to move in
    after_cp: int   # eval of the position they left behind


def mate_as_cp(cp: Optional[int], mate: Optional[int]) -> Optional[int]:
    """Collapse a (cp, mate) eval pair to a single comparable number.

    Returns None when the row carries neither — that position simply has
    no evaluation and must be skipped, never treated as 0.0 (the
    null-not-zero rule: a missing eval is not an equal position).
    """
    if cp is not None:
        return cp
    if mate is not None:
        return MATE_CP if mate > 0 else -MATE_CP
    return None


def win_percent(cp: int) -> float:
    """Lichess's centipawn -> expected-score curve, in percent (0-100).

    The logistic constant is Lichess's published fit over millions of
    games; we reuse it so "accuracy" here means the same thing it means
    on the site the games are imported from.
    """
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0)


def move_accuracy(win_before: float, win_after: float) -> float:
    """Accuracy (0-100) for a single move, from the win-percent it cost.

    Also Lichess's published curve. Clamped, because the exponential
    exceeds 100 for a move that *gains* win probability — playing a move
    the engine underrated is not 104% accurate.
    """
    drop = max(0.0, win_before - win_after)
    raw = 103.1668 * math.exp(-0.04354 * drop) - 3.1669
    return max(0.0, min(100.0, raw))


def centipawn_loss(before_cp: int, after_cp: int) -> int:
    """How much the move gave away, in centipawns. Never negative.

    `classify.py` computes the same quantity signed, as
    `delta_cp = after - before` (user POV, negative = worse). CPL is its
    downside only: a move that improves the position loses nothing.
    """
    return min(MAX_CPL, max(0, before_cp - after_cp))


def summarise(pairs: Sequence[tuple[int, int]]) -> tuple[Optional[float], Optional[int]]:
    """Return (accuracy_percent, acpl) over the user's (before, after) evals.

    `(None, None)` when there is nothing to average — an unanalysed game
    reports "—", never "0% accuracy, 0 ACPL", which would read as a
    perfect game and a catastrophic one at the same time.
    """
    if not pairs:
        return None, None
    accuracies: list[float] = []
    losses: list[int] = []
    for before, after in pairs:
        accuracies.append(move_accuracy(win_percent(before), win_percent(after)))
        losses.append(centipawn_loss(before, after))
    return (
        sum(accuracies) / len(accuracies),
        round(sum(losses) / len(losses)),
    )


def side_to_move_at(ply: int) -> str:
    """Whose move it is at `positions.ply`.

    The journal's convention (see `play.ply_of`): ply 1 is the starting
    position with white to move, so odd plies are white's.
    """
    return "white" if ply % 2 == 1 else "black"


def user_move_evals(
    conn: sqlite3.Connection, *, game_id: int, user_color: str
) -> list[MoveEval]:
    """Every user move in `game_id` that has an eval on both sides of it.

    Moves missing either eval are dropped rather than guessed at, so the
    averages above are over real data only. The "after" eval is the
    rank-1 eval of the *next* position, negated: the journal stores it
    from that position's side-to-move POV, which is the opponent's.
    """
    rows = conn.execute(
        """
        SELECT p.ply AS ply, e.cp AS cp, e.mate AS mate
          FROM positions p
          LEFT JOIN evals e
            ON e.position_id = p.id AND e.multipv_rank = 1
         WHERE p.game_id = ?
         ORDER BY p.ply ASC
        """,
        (game_id,),
    ).fetchall()
    # A position can carry several rank-1 rows if it was re-analysed;
    # last write wins, matching what the review page shows.
    eval_by_ply: dict[int, Optional[int]] = {}
    plies: list[int] = []
    for row in rows:
        if row["ply"] not in eval_by_ply:
            plies.append(row["ply"])
        eval_by_ply[row["ply"]] = mate_as_cp(row["cp"], row["mate"])

    out: list[MoveEval] = []
    for ply in plies:
        if side_to_move_at(ply) != user_color:
            continue
        before = eval_by_ply.get(ply)
        after_opponent_pov = eval_by_ply.get(ply + 1)
        if before is None or after_opponent_pov is None:
            continue
        out.append(MoveEval(ply=ply, before_cp=before, after_cp=-after_opponent_pov))
    return out


__all__ = [
    "MATE_CP",
    "MAX_CPL",
    "MoveEval",
    "mate_as_cp",
    "win_percent",
    "move_accuracy",
    "centipawn_loss",
    "summarise",
    "side_to_move_at",
    "user_move_evals",
    "phase_for_ply",
]
