"""Local, template-based text generation — never Hermes.

Two jobs live here:

1. The hint ladder's tiers 0-3 (ROADMAP §2.1). These MUST NOT depend on
   an LLM: they are the product's core promise ("the app can always
   coach you, even offline, and never leaks the answer for free"). Tier
   4 also builds its text here — the move and PV are known facts from
   the engine, not something an LLM should be asked to phrase, so
   there's no reason to spend an upstream call on it either.
2. A deterministic, always-available review summary (`local_review_summary`)
   used by `GET /review/{id}/summary` whenever the Hermes coach layer is
   unavailable. It must never be an empty string — a silent review is a
   broken one.

Layer discipline (PROJECT.md §3): everything in this module is a string
template over facts the Truth layer already computed (motifs, evals,
squares). It never calls an engine and never talks to a network.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import chess


# --- the hint ladder ------------------------------------------------------

PROCESS_PROMPTS: tuple[str, ...] = (
    "Before anything: what did his last move attack?",
    "Checks, captures, threats — in that order. What do you see?",
    "Your opponent just moved. What does that piece attack now?",
    "Look at every one of your pieces that gives check. Any of them work?",
)

MOTIF_PHRASES: dict[str, str] = {
    "hung_piece": "Something of yours — or his — is simply undefended.",
    "fork": "The idea is a fork. Look for a square that hits two things at once.",
    "pin": "The idea is a pin. One of his pieces can't move without losing something bigger behind it.",
    "skewer": "The idea is a skewer. Attack the front piece and the one behind it falls too.",
    "back_rank": "The idea is a back-rank weakness. Someone's king is short on escape squares.",
    "discovered_attack": "The idea is a discovered attack. Moving one piece unmasks another.",
    "missed_fork": "There was a fork available. Look for a square that hits two things at once.",
    "missed_pin": "There was a pin available. Look for a piece that can't move without losing material behind it.",
}
DEFAULT_MOTIF_PHRASE = "There's a concrete tactical idea here, not just a quiet improving move."


def _region_for_square(square_uci_pair: str) -> str:
    """Region name (queenside/centre/kingside) from a move's destination file."""
    to_square = chess.parse_square(square_uci_pair[2:4])
    file_idx = chess.square_file(to_square)  # 0=a .. 7=h
    if file_idx <= 2:
        return "queenside"
    if file_idx <= 4:
        return "centre"
    return "kingside"


def _piece_name(board_before: chess.Board, uci: str) -> str:
    from_square = chess.parse_square(uci[0:2])
    piece = board_before.piece_at(from_square)
    if piece is None:
        return "piece"
    return chess.piece_name(piece.piece_type)  # "pawn", "knight", ...


@dataclass(frozen=True)
class HintContent:
    text: str
    squares: list[str]
    motif: Optional[str]
    move_uci: Optional[str]
    pv: list[str]


def build_hint(
    tier: int,
    *,
    board_before: chess.Board,
    best_uci: str,
    motifs: list[str],
    pv: list[str],
    ply: int,
) -> HintContent:
    """Build the tier's text/reveal. Never returns move_uci/pv below tier 4.

    `motifs` are the detector's output for this position (may be empty —
    a position can be a plain positional improvement).
    """
    if tier == 0:
        return HintContent(
            text=PROCESS_PROMPTS[ply % len(PROCESS_PROMPTS)],
            squares=[], motif=None, move_uci=None, pv=[],
        )
    if tier == 1:
        region = _region_for_square(best_uci)
        return HintContent(
            text=f"There's something concrete here. It's on the {region}.",
            squares=[], motif=None, move_uci=None, pv=[],
        )
    if tier == 2:
        motif = motifs[0] if motifs else None
        phrase = MOTIF_PHRASES.get(motif, DEFAULT_MOTIF_PHRASE) if motif else DEFAULT_MOTIF_PHRASE
        return HintContent(
            text=phrase, squares=[], motif=motif, move_uci=None, pv=[],
        )
    if tier == 3:
        piece = _piece_name(board_before, best_uci)
        from_sq = best_uci[0:2]
        motif = motifs[0] if motifs else None
        return HintContent(
            text=f"Your {piece} is the piece that does it.",
            squares=[from_sq], motif=motif, move_uci=None, pv=[],
        )
    if tier == 4:
        from_sq, to_sq = best_uci[0:2], best_uci[2:4]
        san = board_before.san(chess.Move.from_uci(best_uci))
        motif = motifs[0] if motifs else None
        pv_text = " ".join(pv) if pv else best_uci
        return HintContent(
            text=(
                f"The move is {san}. Full line: {pv_text}. "
                "This position is queued as a drill so you see it again unassisted."
            ),
            squares=[from_sq, to_sq], motif=motif, move_uci=best_uci, pv=list(pv),
        )
    raise ValueError(f"invalid hint tier: {tier}")


# --- deterministic review summary -----------------------------------------


def local_review_summary(
    *,
    accuracy: float,
    acpl: Optional[int],
    n_mistakes: int,
    n_highlights: int,
    worst_severity: Optional[str],
) -> str:
    """Build a plain-language summary with no LLM, when Hermes is unavailable.

    Deterministic and always non-empty, per the contract's rule that a
    review page never ships an empty or fabricated string.
    """
    acpl_text = f"{acpl} centipawns average loss" if acpl is not None else "no measurable loss"
    parts = [f"Accuracy {accuracy:.1f}%, {acpl_text} across the game."]
    if n_highlights:
        parts.append(
            f"{n_highlights} good moment{'s' if n_highlights != 1 else ''} "
            "worth noticing — see the highlights above the mistake list."
        )
    if n_mistakes:
        sev = f", the worst rated a {worst_severity}" if worst_severity else ""
        parts.append(
            f"{n_mistakes} instructive mistake{'s' if n_mistakes != 1 else ''}"
            f"{sev}. Review each one below with the engine's line."
        )
    else:
        parts.append("No instructive mistakes were found in this game.")
    return " ".join(parts)


__all__ = ["HintContent", "build_hint", "local_review_summary", "PROCESS_PROMPTS", "MOTIF_PHRASES"]
