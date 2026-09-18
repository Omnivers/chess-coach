"""Phase 0 — deterministic motif detection (§6).

These tests pin each detector on a hand-built position so a future refactor
that breaks one of them fails loudly. They are not exhaustive — they're
contracts: "if you build this position, this motif fires."
"""
from __future__ import annotations

import chess

from chess_coach.analysis import (
    _is_fork,
    _is_skewer,
    detect_motifs_after_move,
    detect_motifs_before_move,
)


def test_hung_piece_detected() -> None:
    # White knight on h3 attacked by a black bishop on g4. f1 bishop and
    # g2/f2 pawns removed so nothing defends it. Detector always inspects
    # white's pieces (the user), regardless of whose turn.
    b = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P1b1/7N/PPPP3P/RNBQK2R w KQkq - 0 2")
    played = chess.Move.from_uci("g1h3")  # the move we just played
    motifs = detect_motifs_before_move(b, played, best_move_uci=None)
    assert "hung_piece" in motifs


def test_no_motif_in_quiet_opening() -> None:
    # Position before white plays Bb5: 1.e4 e5 2.Nf3 Nc6 — but it doesn't
    # matter whose turn it is; the detector always inspects white's pieces.
    # Verify the natural position (with f1 bishop and f2 pawn in place)
    # doesn't fire any motif.
    b = chess.Board()
    b.push_uci("e2e4"); b.push_uci("e7e5")
    b.push_uci("g1f3"); b.push_uci("b8c6")
    played = chess.Move.from_uci("f1b5")  # the next move
    motifs = detect_motifs_before_move(b, played, best_move_uci=None)
    assert motifs == []


def test_missed_fork_detected() -> None:
    # Position where the engine's best move creates a knight fork: white
    # knight on b3 can play Nc5 (or similar) to fork black queen and rook.
    # Even simpler — knight on c3 reaches d5 (rook, val 5) and e4 (own pawn)
    # and b5. Not a fork. Knight on b3 reaches a5 (rook, val 5) and d4 (empty)
    # and c5. So knight on b3 → c5 attacks d7 (queen, val 9) and e6 (empty)
    # and b7 (pawn, val 1, lower). Not two higher-value.
    # Use the canonical "knight fork" position: white knight on c3, black
    # queen on d8 and black rook on d7. Knight c3→b5 reaches c7 (val 3) and
    # d6 (val 0?) and a7 (val 1). Not a fork.
    # The reliable fork geometry: place pieces so knight on f3 attacks two
    # higher-value enemy pieces. Knight f3 reaches e5, g5, d4, h4, d2, h2,
    # e1 (own king). Of these, d4 could have a black queen, g5 could have a
    # black rook. Yes: queen on d4 and rook on g5. Knight f3 attacks both.
    b = chess.Board()
    b.remove_piece_at(chess.G1)             # clear white knight from initial
    b.remove_piece_at(chess.D8)             # clear black queen
    b.remove_piece_at(chess.H8)             # clear black rook
    b.set_piece_at(chess.F3, chess.Piece(chess.KNIGHT, chess.WHITE))  # put it on f3 instead
    b.set_piece_at(chess.D4, chess.Piece(chess.QUEEN, chess.BLACK))
    b.set_piece_at(chess.G5, chess.Piece(chess.ROOK, chess.BLACK))
    # Sanity check the setup.
    assert b.piece_type_at(chess.F3) == chess.KNIGHT
    assert b.color_at(chess.F3) == chess.WHITE
    assert b.piece_type_at(chess.D4) == chess.QUEEN
    assert b.piece_type_at(chess.G5) == chess.ROOK
    # Verify the move we're claiming as "best" creates the fork:
    # White plays some other move first (no-op for the test) — actually the
    # missed-tactic detector wants to know: given that the engine's best was
    # a move (not necessarily what we just played), does that best move
    # create a fork? Here white to move — engine's best is "what?" We need a
    # legal move that creates a fork. Knight f3 → e5 doesn't reach d4 or g5.
    # f3 → d4 captures the queen. f3 → g5 captures the rook. We need a
    # non-capturing move that creates a fork. Knight f3 → e5 attacks d7, f7,
    # g6, g4, d3, c4, c6 — none of the black pieces. Try h4 — attacks f5,
    # g6, f3 (own). Hmm.
    # Easier: use a knight on e2 that can play Nf4 (own) → not a fork. Or
    # knight on c3 that can play Nd5, attacking black queen on e7 and rook
    # on f6. Knight c3 → d5 reaches e7 (knight L 1+2) and f6 (knight L 2+1)
    # and b6, b4, e1 (own king). Yes! So: knight on c3, black queen on e7,
    # black rook on f6. Nd5 forks both.
    b = chess.Board()
    b.remove_piece_at(chess.B1)             # clear white knight from initial
    b.remove_piece_at(chess.D8)             # clear black queen
    b.remove_piece_at(chess.H8)             # clear black rook
    b.set_piece_at(chess.C3, chess.Piece(chess.KNIGHT, chess.WHITE))
    b.set_piece_at(chess.E7, chess.Piece(chess.QUEEN, chess.BLACK))
    b.set_piece_at(chess.F6, chess.Piece(chess.ROOK, chess.BLACK))
    assert b.piece_type_at(chess.C3) == chess.KNIGHT
    assert b.color_at(chess.C3) == chess.WHITE
    assert b.piece_type_at(chess.E7) == chess.QUEEN
    assert b.piece_type_at(chess.F6) == chess.ROOK
    # Verify c3d5 is legal and creates a fork.
    assert chess.Move.from_uci("c3d5") in b.legal_moves
    # White's actually-played move (the user moved something else; we just
    # care about the detector finding the missed fork via best_move_uci).
    played = chess.Move.from_uci("g1f3")
    motifs = detect_motifs_before_move(b, played, best_move_uci="c3d5")
    assert "missed_fork" in motifs


def test_discovered_attack_after_move() -> None:
    # Position: white rook on d1, white knight on d2 (blocking the rook's view
    # of d7). Black queen on d7. After the knight moves (d2→d4), the rook
    # suddenly attacks the queen — a discovered attack.
    b = chess.Board()
    # Move the d2 pawn out of the way so the knight can sit there, and clear
    # the d1 queen to put a rook.
    b.remove_piece_at(chess.D2)            # remove white d2 pawn
    b.remove_piece_at(chess.D1)            # remove white queen
    b.remove_piece_at(chess.G1)            # remove white g1 knight (so f3 is free for d2-f3)
    b.remove_piece_at(chess.E8)            # remove black king so we can put a queen elsewhere
    # Place the new pieces.
    b.set_piece_at(chess.D1, chess.Piece(chess.ROOK, chess.WHITE))
    b.set_piece_at(chess.D2, chess.Piece(chess.KNIGHT, chess.WHITE))
    # Re-place black king on e8 (replacing what we removed).
    b.set_piece_at(chess.E8, chess.Piece(chess.KING, chess.BLACK))
    # Place the black queen on d7.
    b.set_piece_at(chess.D7, chess.Piece(chess.QUEEN, chess.BLACK))
    assert b.piece_type_at(chess.D1) == chess.ROOK
    assert b.piece_type_at(chess.D2) == chess.KNIGHT
    assert b.piece_type_at(chess.D7) == chess.QUEEN
    # The rook can't see d7 yet (blocked by knight on d2).
    assert not b.is_attacked_by(chess.WHITE, chess.D7)
    played = chess.Move.from_uci("d2f3")
    # `detect_motifs_after_move` expects `board_after` to be the post-move
    # state, so push the move first.
    b.push(played)
    motifs = detect_motifs_after_move(b, played)
    assert "discovered_attack" in motifs

# --- Regression: king-as-target. These four positions all failed before the
# --- fix that ranked the king as the strongest tactical target (2026-09-18).

def test_royal_fork_is_detected():
    """Knight forking king and queen is THE canonical fork — must not be skipped."""
    board = chess.Board("2q3k1/4N3/8/8/8/8/8/K7 b - - 0 1")
    assert _is_fork(board, chess.E7)


def test_fork_of_equal_value_undefended_pieces():
    """Knight forking two undefended bishops wins a piece, though neither outranks it."""
    board = chess.Board("6k1/8/1b3b2/3N4/8/8/8/K7 b - - 0 1")
    assert _is_fork(board, chess.D5)


def test_fork_is_attributed_to_the_forking_piece_only():
    """Na1 attacks nothing; the queen's attacks must not be credited to it."""
    board = chess.Board("3r2k1/8/8/8/3Q3r/8/8/N5K1 b - - 0 1")
    assert not _is_fork(board, chess.A1)


def test_classic_skewer_king_in_front_of_queen():
    """Ra1 checks Ka4; the king moves and the rook takes Qa8. The classic skewer."""
    board = chess.Board("q7/8/8/8/k7/8/8/R6K b - - 0 1")
    assert _is_skewer(board, chess.A1)


def test_pin_shape_is_not_a_skewer():
    """Knight shielding its king is a pin, not a skewer: front < back."""
    board = chess.Board("k7/8/8/8/8/8/8/R2n3K b - - 0 1")
    assert not _is_skewer(board, chess.A1)
