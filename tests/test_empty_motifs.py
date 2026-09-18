"""Phase 1 — empty motifs are valid mistake rows.

Phase 0 originally required motifs to be non-empty. The Phase 1
analyzer can legitimately find a real mistake/inaccuracy where no
deterministic motif fired (e.g. a quiet positional error). Those rows
still matter for the journal — they're the negative class for fitting
the instructive filter, and the position is reviewable — so empty
motifs are allowed. Phase 2 narration will fill the gap.
"""
from __future__ import annotations

from pathlib import Path

from chess_coach import journal as j


def test_empty_motifs_allowed(tmp_path: Path) -> None:
    journal = j.open_journal(tmp_path / "empty_motifs.db")
    with journal.transaction() as conn:
        gid = j.insert_game(
            conn, source="lichess", external_id=None,
            played_at="2026-09-17T20:00:00+00:00",
            color="white", result="loss", pgn="...",
        )
        pid = j.insert_position(conn, game_id=gid, ply=1, fen="startpos")
        mid = j.insert_mistake(
            conn, position_id=pid, game_id=gid,
            delta_cp=-200, severity="mistake",
            classification="positional", phase="middlegame",
            motifs=[],  # no deterministic motif — Phase 2 narration handles it
            engine_best_uci="e2e4", played_uci="d1h5",
            instructive=False,
            threshold_cp_in_force=115,
            rejection_reason="not_findable",
        )
    assert mid > 0
    with journal.read() as conn:
        row = conn.execute("SELECT motifs FROM mistakes WHERE id = ?", (mid,)).fetchone()
    assert row["motifs"] == ""


def test_empty_string_motifs_allowed(tmp_path: Path) -> None:
    journal = j.open_journal(tmp_path / "empty_string_motifs.db")
    with journal.transaction() as conn:
        gid = j.insert_game(
            conn, source="lichess", external_id=None,
            played_at="2026-09-17T20:00:00+00:00",
            color="white", result="loss", pgn="...",
        )
        pid = j.insert_position(conn, game_id=gid, ply=1, fen="startpos")
        mid = j.insert_mistake(
            conn, position_id=pid, game_id=gid,
            delta_cp=-150, severity="mistake",
            classification="positional", phase="middlegame",
            motifs="",  # same idea, string form
            engine_best_uci="e2e4", played_uci="d1h5",
            instructive=False,
            threshold_cp_in_force=115,
        )
    assert mid > 0
