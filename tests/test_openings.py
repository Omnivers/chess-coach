"""Phase 5.3 — the opening-principles primer (`chess_coach/openings.py`,
`chess_coach/api_openings.py`).

`openings.py` derives every FEN by replaying SAN through python-chess at
import time rather than storing hand-authored FENs, specifically so a
typo'd move fails loudly instead of shipping a wrong position to someone
who told us they don't know openings and has no way to notice. These
tests re-verify that contract independently of import succeeding (import
succeeding only proves the SAN was *legal*, not that the stored FEN
matches what was actually played), and check the one factual chess claim
the primer makes (the Scholar's Mate threat) against python-chess rather
than trusting the author's memory.

Pure: no engine, no network, no journal.
"""
from __future__ import annotations

from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient

from chess_coach.api import create_app
from chess_coach.journal import open_journal
from chess_coach.openings import LINES, PRINCIPLES, get_primer


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """No engine/analysis involved — `/openings/primer` must work standalone."""
    j = open_journal(tmp_path / "openings-api.db")
    return TestClient(create_app(j))


def test_every_san_replays_legally_from_the_start() -> None:
    """The whole point of storing SAN-not-FEN: a broken line must be
    detectable by replaying it, independent of whatever `_build()` did at
    import time."""
    for line in LINES:
        board = chess.Board()
        for ply in line.plies:
            board.push_san(ply.san)  # raises ValueError/IllegalMoveError if bad


def test_every_stored_fen_matches_an_independent_replay() -> None:
    """Protects against a future refactor that starts authoring FENs by
    hand: the stored `ply.fen` must equal what python-chess produces when
    walking the same SAN sequence from scratch."""
    for line in LINES:
        board = chess.Board()
        for ply in line.plies:
            board.push_san(ply.san)
            assert board.fen() == ply.fen


def test_ply_numbering_is_one_based_contiguous_and_alternates_side() -> None:
    for line in LINES:
        for i, ply in enumerate(line.plies, start=1):
            assert ply.ply == i
            expected_side = "white" if i % 2 == 1 else "black"
            assert ply.side == expected_side


def test_every_principle_id_referenced_by_a_ply_exists() -> None:
    """A dangling principle_id would render a blank card in the UI — the
    frontend looks up the principle by id to show its title next to the
    move's note."""
    known_ids = {p.id for p in PRINCIPLES}
    for line in LINES:
        for ply in line.plies:
            if ply.principle_id is not None:
                assert ply.principle_id in known_ids


def test_line_ids_are_unique() -> None:
    ids = [line.id for line in LINES]
    assert len(ids) == len(set(ids))


def test_principle_ids_are_unique() -> None:
    ids = [p.id for p in PRINCIPLES]
    assert len(ids) == len(set(ids))


def test_get_primer_matches_module_content() -> None:
    primer = get_primer()
    assert len(primer["principles"]) == len(PRINCIPLES)
    assert len(primer["lines"]) == len(LINES)
    assert {p["id"] for p in primer["principles"]} == {p.id for p in PRINCIPLES}
    assert {l["id"] for l in primer["lines"]} == {l.id for l in LINES}


def test_scholars_mate_line_actually_threatens_mate_on_f7() -> None:
    """The primer tells a beginner "Qxf7 would be mate" after 1.e4 e5
    2.Bc4 Nc6 3.Qh5. That is a factual chess claim made to someone who
    explicitly does not know openings, so it must be checked by
    python-chess rather than taken on the author's word. Ply 5 is the
    position right after 3.Qh5, with Black to move; flipping the side to
    move to White (never authored by hand — derived from the same FEN
    the primer already stores) and playing Qxf7 must be checkmate."""
    line = next(l for l in LINES if l.id == "scholars-mate")
    ply_after_qh5 = line.plies[4]
    assert ply_after_qh5.san == "Qh5"

    fields = ply_after_qh5.fen.split(" ")
    assert fields[1] == "b"  # Black to move, as expected after White's 3rd move
    fields[1] = "w"
    board = chess.Board(" ".join(fields))

    board.push_san("Qxf7")
    assert board.is_checkmate()


def test_openings_primer_endpoint_returns_full_content(client: TestClient) -> None:
    """No engine, no live game, no journal seeding — this endpoint must
    serve its full static content regardless."""
    resp = client.get("/openings/primer")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["principles"]) == len(PRINCIPLES)
    assert len(body["lines"]) == len(LINES)
    scholars = next(l for l in body["lines"] if l["id"] == "scholars-mate")
    assert scholars["plies"][4]["san"] == "Qh5"
    assert scholars["plies"][4]["note"]
