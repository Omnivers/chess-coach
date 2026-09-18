"""A beginner's opening *primer* — principles plus a handful of model lines.

This is deliberately NOT ROADMAP.md §4 (Phase 9, the data-driven repertoire
built from the Lichess Opening Explorer and the user's own games). It is
what ROADMAP.md §5.3 tutorials calls "opening principles — centre,
development, king safety. Not lines." The four lines below exist only to
make the principles concrete; they are illustration, not a repertoire to
memorise, and they carry no theory-table pretensions.

The whole module is shaped around one constraint: **no FEN is ever hand
authored.** Every line is stored as an ordered list of `(san, note,
principle_id)` triples and replayed through `python-chess` at import time
(`_build()`, called once at module load) to derive the FEN after each ply.
A typo'd SAN string — "Nf6" written as "Nd6" by mistake — then raises
`ValueError`/`chess.IllegalMoveError` on `import chess_coach.openings`
instead of silently shipping a wrong board position to someone who has
told us outright that they don't know openings and has no way to notice
the mistake themselves. Truth about the position is python-chess's job,
never the author's memory — see PROJECT.md's engine-is-truth layering.

Nothing here touches Stockfish, an LLM, or the network. It is static
content plus board replay, full stop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import chess


@dataclass(frozen=True)
class Principle:
    id: str
    title: str
    body: str


@dataclass(frozen=True)
class PrimerPly:
    ply: int  # 1-based
    san: str
    fen: str  # position AFTER this move; derived, never authored
    side: str  # "white" | "black"
    note: Optional[str]
    principle_id: Optional[str]


@dataclass(frozen=True)
class PrimerLine:
    id: str
    title: str
    subtitle: str
    perspective: str  # "white" | "black" — whose plan the notes explain
    start_fen: str
    plies: tuple[PrimerPly, ...]


PRINCIPLES: tuple[Principle, ...] = (
    Principle(
        id="centre",
        title="Take the centre",
        body=(
            "The four squares in the middle are worth more than any others: a "
            "piece there touches more of the board. Put a pawn on e4 or d4 (e5 "
            "or d5 as Black) and fight to keep it."
        ),
    ),
    Principle(
        id="develop",
        title="Get your pieces out",
        body=(
            "Knights and bishops do nothing on the back rank. Aim to have both "
            "knights and both bishops off their starting squares before you "
            "start attacking. Knights belong toward the centre, not the edge."
        ),
    ),
    Principle(
        id="king-safety",
        title="Castle early",
        body=(
            "A king left in the centre is the thing your opponent is aiming "
            "at. Castling takes one move, tucks the king away and connects "
            "the rooks. Do it by about move ten."
        ),
    ),
    Principle(
        id="one-move-each",
        title="Don't move the same piece twice",
        body=(
            "Every move you spend moving a piece a second time is a move your "
            "opponent spends developing a new one. Unless there is a concrete "
            "reason, move each piece once and move on to the next."
        ),
    ),
    Principle(
        id="queen-late",
        title="Keep the queen home for now",
        body=(
            "An early queen looks aggressive and is easy to attack. Every "
            "time it gets chased you lose a move and your opponent gains one. "
            "Bring it out once the minor pieces are already working."
        ),
    ),
)

# One (san, note, principle_id) triple per ply, in game order. Kept private —
# `_build()` is the only thing that reads this table, so the public LINES
# tuple below is always the replayed-and-verified version, never this raw
# table straight off the page.
_LINE_TABLES: tuple[tuple[str, str, str, str, tuple[tuple[str, Optional[str], Optional[str]], ...]], ...] = (
    (
        "white-italian",
        "As White: the Italian setup",
        "One plan you can play against almost anything Black tries.",
        "white",
        (
            ("e4", "A pawn in the centre on move one. It also opens lines for the bishop on f1 and the queen.", "centre"),
            ("e5", "Black stakes the same claim. If Black plays something else, your next three moves barely change.", None),
            ("Nf3", "Develops a piece and attacks the e5 pawn at the same time. A knight on f3 is doing work; a knight on h3 is not.", "develop"),
            ("Nc6", None, None),
            ("Bc4", "The bishop's best diagonal. It eyes f7 — the one pawn in Black's position that nothing but the king defends, which is why so many beginner attacks aim there.", "develop"),
            ("Bc5", None, None),
            ("c3", "Quiet but useful: it supports a later d4 and gives the bishop on c4 a retreat square on c2.", None),
            ("Nf6", None, None),
            ("d3", "Solid. It frees the dark-squared bishop and holds e4. d4 is possible here too, but d3 is the version you can play without knowing theory.", "centre"),
            ("d6", None, None),
            ("O-O", "King safe, rook connected, and every minor piece on the kingside already developed. This is the position you were aiming for.", "king-safety"),
            ("O-O", None, None),
        ),
    ),
    (
        "black-vs-e4",
        "As Black against 1.e4: mirror it",
        "The same plan, one move behind. Nothing new to memorise.",
        "black",
        (
            ("e4", None, None),
            ("e5", "Meet a centre pawn with a centre pawn. Black is a move behind all game, so the goal is a position you understand, not an advantage.", "centre"),
            ("Nf3", None, None),
            ("Nc6", "Defends e5 and develops. This is the move order to learn: the pawn first, then the knight that defends it.", "develop"),
            ("Bc4", None, None),
            ("Bc5", "Mirrors White. Note it also stops White from playing an easy d4.", "develop"),
            ("d3", None, None),
            ("Nf6", "The last minor piece that can come out quickly, and it hits e4.", "develop"),
            ("O-O", None, None),
            ("O-O", "Same finish line as the White version: four minor pieces out, king castled, nothing hanging.", "king-safety"),
        ),
    ),
    (
        "black-vs-d4",
        "As Black against 1.d4: a solid centre",
        "When the e-pawn plan doesn't apply.",
        "black",
        (
            ("d4", None, None),
            ("d5", "Same idea as against 1.e4 — answer the centre pawn with a centre pawn.", "centre"),
            ("c4", "White offers a pawn to pull your d5 pawn off the centre. You do not have to take it.", None),
            ("e6", "Declining. The pawn now defends d5 and the f8 bishop has somewhere to go. The cost is that the c8 bishop is shut in for a while — that is the trade.", "centre"),
            ("Nc3", None, None),
            ("Nf6", "Develops and adds a third defender to d5.", "develop"),
            ("Nf3", None, None),
            ("Be7", "Modest, but it is the move that lets you castle next. That is enough of a reason.", "develop"),
            ("Bf4", None, None),
            ("O-O", "Solid, no weaknesses, nothing to memorise past this point.", "king-safety"),
        ),
    ),
    (
        "scholars-mate",
        "The four-move mate, and how to stop it",
        "The single most common way an 800-rated game ends. It should never work on you again.",
        "black",
        (
            ("e4", None, None),
            ("e5", None, None),
            ("Bc4", "The bishop aims at f7. On its own this is a completely normal move.", None),
            ("Nc6", "Normal development. Nothing has gone wrong yet.", "develop"),
            ("Qh5", "Now it is a threat: the queen and the bishop both hit f7, and f7 is defended only by the king. Qxf7 would be mate.", "queen-late"),
            ("g6", "The move to remember. It attacks the queen and blocks the diagonal, and it does both in one move — so you are not losing time to defend.", "king-safety"),
            ("Qf3", "The queen keeps aiming at f7, now supported by the bishop.", None),
            ("Nf6", "Develops a piece *and* covers the mate. That is the pattern: find the defence that is also a developing move.", "develop"),
            ("Qe2", "Nothing came of it. White's queen has now moved three times, and White has one piece developed to your two.", "one-move-each"),
            ("Bc5", "Black is simply better here — not because of a trick, but because White spent the opening moving one piece.", "develop"),
        ),
    ),
)


def _build() -> tuple[PrimerLine, ...]:
    """Replay every line table through a fresh board, one ply at a time.

    `board.push_san` raises `ValueError` (or its subclass
    `chess.IllegalMoveError`) on anything illegal or malformed, and that
    exception is left to propagate — a broken primer must fail loudly at
    import, not hand a beginner a board that doesn't match the notes.
    """
    lines = []
    for line_id, title, subtitle, perspective, moves in _LINE_TABLES:
        board = chess.Board()
        plies = []
        for i, (san, note, principle_id) in enumerate(moves, start=1):
            board.push_san(san)
            side = "white" if i % 2 == 1 else "black"
            plies.append(
                PrimerPly(
                    ply=i,
                    san=san,
                    fen=board.fen(),
                    side=side,
                    note=note,
                    principle_id=principle_id,
                )
            )
        lines.append(
            PrimerLine(
                id=line_id,
                title=title,
                subtitle=subtitle,
                perspective=perspective,
                start_fen=chess.STARTING_FEN,
                plies=tuple(plies),
            )
        )
    return tuple(lines)


LINES: tuple[PrimerLine, ...] = _build()


def get_primer() -> dict:
    """Plain-JSON-ready shape for `GET /openings/primer`."""
    return {
        "principles": [
            {"id": p.id, "title": p.title, "body": p.body} for p in PRINCIPLES
        ],
        "lines": [
            {
                "id": line.id,
                "title": line.title,
                "subtitle": line.subtitle,
                "perspective": line.perspective,
                "start_fen": line.start_fen,
                "plies": [
                    {
                        "ply": ply.ply,
                        "san": ply.san,
                        "fen": ply.fen,
                        "side": ply.side,
                        "note": ply.note,
                        "principle_id": ply.principle_id,
                    }
                    for ply in line.plies
                ],
            }
            for line in LINES
        ],
    }


__all__ = ["Principle", "PrimerPly", "PrimerLine", "PRINCIPLES", "LINES", "get_primer"]
