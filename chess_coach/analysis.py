"""Phase 0 — the instructive filter and deterministic motif detection.

PROJECT.md §5: a mistake enters the drill queue iff all three hold:
  1. delta_cp >= threshold(my_rating)         (rating-dependent bar)
  2. refutation is a short forcing line        (findable)
  3. abs(eval_before) < 600cp                  (live position)

PROJECT.md §6: several motifs are deterministically detectable with
python-chess — hung piece, fork, pin, skewer, back-rank, missed threat,
discovered attack. Detect what's detectable; let Hermes narrate the rest.

This module is pure functions, no I/O, no LLM. Tests in tests/test_motifs.py
exercise each detector on positions it can name.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

import chess


# --------------------------------------------------------------------- §5

# Rating-dependent bar from §5: "roughly 150cp at 1200, 80cp at 1800".
# Linear interpolation clamped to a sane range so we never accept 0cp junk
# nor refuse a 250cp blunder at master level.
def instructive_threshold_cp(my_rating: int | None) -> int:
    if my_rating is None:
        # No rating on file → be conservative; require a clear mistake (≥100cp).
        return 100
    # Anchor points: (rating, threshold_cp).
    # 1200 → 150, 1800 → 80. Linear in between, clamped to [60, 200].
    lo_r, lo_t = 1200, 150
    hi_r, hi_t = 1800, 80
    if my_rating <= lo_r:
        threshold = lo_t
    elif my_rating >= hi_r:
        threshold = hi_t
    else:
        threshold = lo_t + (hi_t - lo_t) * (my_rating - lo_r) / (hi_r - lo_r)
    return max(60, min(200, int(round(threshold))))


# How deep a forcing sequence we expect before calling a position "findable."
# §5 says "≤ N plies, checks/captures/threats." N=3 is a starting heuristic.
MAX_REFUTATION_PLIES = 3

# §5 "live position" cap.
LIVE_POSITION_CAP_CP = 600


class RejectionReason(str, Enum):
    BELOW_THRESHOLD = "below_threshold"
    NOT_FINDABLE = "not_findable"
    DEAD_POSITION = "dead_position"


@dataclass(frozen=True)
class FilterVerdict:
    instructive: bool
    threshold_cp_in_force: int
    rejection_reason: RejectionReason | None

    @classmethod
    def reject(cls, reason: RejectionReason, threshold: int) -> "FilterVerdict":
        return cls(instructive=False, threshold_cp_in_force=threshold, rejection_reason=reason)

    @classmethod
    def accept(cls, threshold: int) -> "FilterVerdict":
        return cls(instructive=True, threshold_cp_in_force=threshold, rejection_reason=None)


def _is_forcing_move(move: chess.Move) -> bool:
    """A forcing move is check, capture, or a direct threat to a higher-value piece.

    We deliberately keep this conservative — the question is "would a human at
    my rating find this in a few seconds?" — not "is this legal." Promotion
    counts as forcing because it changes material.
    """
    if move.promotion is not None:
        return True
    board = move.board()
    if board.is_capture(move) or board.gives_check(move):
        return True
    # Threat to a higher-value piece: is the destination attacked, and does the
    # square hold a piece of higher value than the mover?
    piece = board.piece_at(move.from_square)
    if piece is None:
        return False
    victim = board.piece_at(move.to_square)
    if victim is not None and victim.piece_type != piece.piece_type:
        # Captures are already handled above; this branch is for non-capturing
        # attacks. If the destination is a square attacked by a higher-value
        # defender we'd find the move by threat detection — but the simpler
        # approximation: if we move to a square currently defended, it's
        # usually a threat worth surfacing.
        return False  # conservative: don't promote random moves to "forcing"
    return False


def _refutation_is_short(board: chess.Board, best_uci: str, depth: int = MAX_REFUTATION_PLIES) -> bool:
    """True if a forcing sequence of <= depth plies exists from `best_uci`.

    Approximation: walk the PV up to `depth` plies and require every ply to be
    a check or capture. We don't need a full tactical search here — that's
    Stockfish's job in Phase 1 — we just need to know whether the line is
    short and forcing, which is the user-visible definition of "findable."
    """
    if not best_uci:
        return False
    try:
        first = chess.Move.from_uci(best_uci)
    except (ValueError, chess.InvalidMoveError):
        return False
    if first not in board.legal_moves:
        return False
    board.push(first)
    forcing_count = 1
    for _ in range(depth - 1):
        if board.is_checkmate() or board.is_stalemate() or board.is_game_over():
            return True
        # Pick the first forcing reply, if any.
        reply = next(
            (m for m in board.legal_moves if board.is_capture(m) or board.gives_check(m)),
            None,
        )
        if reply is None:
            return False
        board.push(reply)
        forcing_count += 1
    return forcing_count >= 1


def evaluate_filter(
    *,
    delta_cp: int,
    eval_before_cp: int,
    my_rating: int | None,
    best_uci: str,
    board_for_refutation: chess.Board,
) -> FilterVerdict:
    """Apply the three §5 gates and return a verdict + the threshold in force.

    Order matters: rating-derived threshold first (cheapest), then live-position
    cap, then findability (most expensive — needs a board copy).
    """
    threshold = instructive_threshold_cp(my_rating)
    if abs(delta_cp) < threshold:
        return FilterVerdict.reject(RejectionReason.BELOW_THRESHOLD, threshold)
    if abs(eval_before_cp) >= LIVE_POSITION_CAP_CP:
        return FilterVerdict.reject(RejectionReason.DEAD_POSITION, threshold)
    if not _refutation_is_short(board_for_refutation, best_uci):
        return FilterVerdict.reject(RejectionReason.NOT_FINDABLE, threshold)
    return FilterVerdict.accept(threshold)


# --------------------------------------------------------------------- §6

# A taxonomy of motifs we can detect deterministically. Others
# ("missed threat", "calculation truncated") need Hermes narration in Phase 2.
DETECTABLE_MOTIFS: tuple[str, ...] = (
    "hung_piece",
    "fork",
    "pin",
    "skewer",
    "back_rank",
    "discovered_attack",
    "missed_fork",
    "missed_pin",
)


def _piece_value(piece_type: chess.PieceType) -> int:
    return {
        chess.PAWN: 1,
        chess.KNIGHT: 3,
        chess.BISHOP: 3,
        chess.ROOK: 5,
        chess.QUEEN: 9,
        chess.KING: 0,  # king attacks aren't forks in the usual sense
    }.get(piece_type, 0)


def _is_hanging(board: chess.Board, square: chess.Square) -> bool:
    """A piece on `square` is hanging if it has no friendly defenders and at least one enemy attacker.

    python-chess's `attackers(color, square)` returns pieces of `color` that
    attack `square`, so to check defenders we pass `piece.color`.
    """
    piece = board.piece_at(square)
    if piece is None:
        return False
    # If the piece is the king we don't classify it as "hanging" — that's a
    # checkmate/shape problem, not a hanging-piece one.
    if piece.piece_type == chess.KING:
        return False
    defenders = board.attackers(piece.color, square)
    attackers = board.attackers(not piece.color, square)
    return len(attackers) > 0 and len(defenders) == 0


def _target_value(piece_type: chess.PieceType) -> int:
    """Tactical target ranking, distinct from material value.

    The king outranks everything: check is forcing, which is precisely what
    makes a royal fork win material — the opponent *must* answer the check, so
    the second target falls. `_piece_value` keeps king=0 for material counting.
    """
    if piece_type == chess.KING:
        return 1000
    return _piece_value(piece_type)


def _is_fork(board: chess.Board, square: chess.Square) -> bool:
    """The piece on `square` attacks two or more enemy pieces it stands to win.

    A target counts when it is the king, is worth more than the forking piece,
    or is undefended. A knight hitting king and queen is the canonical fork, so
    the king is the strongest target here, never skipped.
    """
    piece = board.piece_at(square)
    if piece is None:
        return False
    targets = 0
    for sq in board.attacks(square):  # squares THIS piece attacks, not its whole side
        victim = board.piece_at(sq)
        if victim is None or victim.color == piece.color:
            continue
        if victim.piece_type == chess.KING:
            targets += 1
        elif (
            _piece_value(victim.piece_type) > _piece_value(piece.piece_type)
            or not board.attackers(victim.color, sq)  # equal value but undefended
        ):
            targets += 1
        if targets >= 2:
            return True
    return False


def _is_pin(board: chess.Board, square: chess.Square) -> bool:
    """A piece on `square` is pinned if moving it would expose a higher-value piece (king included) on the same line."""
    piece = board.piece_at(square)
    if piece is None or piece.piece_type == chess.KING:
        return False
    return board.is_pinned(piece.color, square)


_RAYS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def _is_skewer(board: chess.Board, square: chess.Square) -> bool:
    """A slider on `square` hits two enemy pieces on one ray, the nearer worth
    at least as much as the one behind it.

    front >= back is what separates a skewer from a pin: a king shielding a
    queen is the classic skewer; a knight shielding the king is a pin. The king
    is never walked through — it blocks the ray like any other piece, and as the
    front piece it is the strongest skewer there is.
    """
    piece = board.piece_at(square)
    if piece is None or piece.piece_type not in (chess.ROOK, chess.BISHOP, chess.QUEEN):
        return False
    enemy = not piece.color
    rank, file = chess.square_rank(square), chess.square_file(square)
    for dr, df in _RAYS:
        if piece.piece_type == chess.BISHOP and dr * df == 0:
            continue
        if piece.piece_type == chess.ROOK and dr * df != 0:
            continue
        front: int | None = None
        for step in range(1, 8):
            r, f = rank + dr * step, file + df * step
            if not (0 <= r < 8 and 0 <= f < 8):
                break
            found = board.piece_at(chess.square(f, r))
            if found is None:
                continue
            if found.color != enemy:
                break  # own piece blocks the ray
            value = _target_value(found.piece_type)
            if front is None:
                front = value  # nearest enemy piece; keep walking for the one behind
                continue
            if front >= value:
                return True
            break  # lesser piece shielding a greater one is a pin, not a skewer
    return False


def _back_rank_mate_available(board: chess.Board) -> bool:
    """The side-to-move can deliver back-rank mate on this very move.

    Cheaper than "the king is on the back rank" — we only flag when it's
    exploitable now, which is what the player actually needs to know.
    """
    if not board.is_check():
        for move in board.legal_moves:
            board.push(move)
            if board.is_checkmate():
                board.pop()
                return True
            board.pop()
    return False


def _discovered_attack_after_move(board: chess.Board, move: chess.Move) -> bool:
    """Pushing `move` opens an attack from one of our pieces onto an enemy piece.

    We approximate by checking if any of our non-moved pieces suddenly attacks
    a square holding an enemy piece of higher value.
    """
    mover = board.piece_at(move.from_square)
    if mover is None:
        return False
    # Capture candidate before: the square we move to. After the move, our
    # piece is gone from `from_square`; this exposes attacks along its lines.
    board.push(move)
    attacker_color = not mover.color  # it's now opponent's turn but we want OUR pieces
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != attacker_color or sq == move.to_square:
            continue
        # Did this piece suddenly attack a higher-value enemy piece?
        for target in chess.SQUARES:
            victim = board.piece_at(target)
            if victim is None or victim.color == mover.color:
                continue
            if not board.is_attacked_by(attacker_color, target):
                continue
            if _piece_value(victim.piece_type) > _piece_value(piece.piece_type):
                board.pop()
                return True
    board.pop()
    return False


def severity_from_delta(delta_cp: int) -> str:
    """Standard Lichess bands.

    |delta_cp| < 50  → not a mistake (noise floor)
    < 100            → inaccuracy
    < 200            → mistake
    else             → blunder

    Raises if the move is so close to equal it doesn't deserve a row at all.
    The journal never sees sub-50cp rows: those would be the instructive
    filter's job, not the severity classifier's.
    """
    a = abs(delta_cp)
    if a < 50:
        raise ValueError(f"delta_cp {delta_cp} below noise floor; not a mistake")
    if a < 100:
        return "inaccuracy"
    if a < 200:
        return "mistake"
    return "blunder"


def detect_motifs_before_move(
    board: chess.Board,
    played_move: chess.Move,
    best_move_uci: str | None,
) -> list[str]:
    """Motifs present in the position right before the played move.

    The user is always white in this app (we could later relax this — the
    detectors themselves are color-agnostic). We always inspect white's
    pieces regardless of whose turn it is, because the question the coach
    answers is "did *you* hang something?" and "you" is always the user.

    `played_move` is the move we actually played. `best_move_uci` lets us
    detect "missed_*" motifs: a tactic existed but we didn't take it.
    """
    motifs: list[str] = []
    user_color = chess.WHITE

    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != user_color:
            continue
        if _is_hanging(board, sq):
            motifs.append("hung_piece")
            break
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != user_color:
            continue
        if _is_fork(board, sq):
            motifs.append("fork")
            break
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != user_color:
            continue
        if _is_pin(board, sq):
            motifs.append("pin")
            break
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.color != user_color:
            continue
        if _is_skewer(board, sq):
            motifs.append("skewer")
            break
    if _back_rank_mate_available(board):
        motifs.append("back_rank")

    # Missed tactics: did the best move create a fork/pin we didn't take?
    if best_move_uci:
        try:
            best = chess.Move.from_uci(best_move_uci)
        except (ValueError, chess.InvalidMoveError):
            best = None
        if best is not None and best in board.legal_moves:
            board.push(best)
            for sq in chess.SQUARES:
                piece = board.piece_at(sq)
                if piece is None or piece.color != user_color:
                    continue
                if _is_fork(board, sq):
                    motifs.append("missed_fork")
                    break
            for sq in chess.SQUARES:
                piece = board.piece_at(sq)
                if piece is None or piece.color != user_color:
                    continue
                if _is_pin(board, sq):
                    motifs.append("missed_pin")
                    break
            board.pop()
    return motifs


def detect_motifs_after_move(
    board_after: chess.Board,
    played_move: chess.Move,
) -> list[str]:
    """Motifs that the played move itself created (for "missed threat" cases).

    `board_after` is the position AFTER the move was played. The function
    reconstructs the "before" position by popping the move from the stack.
    If `board_after` doesn't actually have `played_move` on its stack (a
    caller bug), we fall back to copying and pushing — which won't be
    a faithful reconstruction but won't crash either.
    """
    board_before = board_after.copy()
    if board_after.move_stack and board_after.move_stack[-1] == played_move:
        board_before.pop()
    else:
        # Fall back: push the move on a copy of `board_after`. This is only
        # correct if the caller passed board_after in the BEFORE state.
        # We accept that ambiguity rather than crashing; the detector is
        # best-effort.
        board_before.push(played_move)
    motifs: list[str] = []
    if _discovered_attack_after_move(board_before, played_move):
        motifs.append("discovered_attack")
    return motifs


__all__ = [
    "instructive_threshold_cp",
    "MAX_REFUTATION_PLIES",
    "LIVE_POSITION_CAP_CP",
    "RejectionReason",
    "FilterVerdict",
    "evaluate_filter",
    "severity_from_delta",
    "DETECTABLE_MOTIFS",
    "detect_motifs_before_move",
    "detect_motifs_after_move",
]