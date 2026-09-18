"""Phase 0 — journal schema and repository behaviour.

Every test here is a contract:
- Schema is created on first open and reused thereafter.
- (source, external_id) is UNIQUE — re-import upserts.
- (game_id, ply) is UNIQUE — replaying a position updates it.
- Rejected mistakes get a rejection_reason; instructive ones don't.
- Every mistake stores the threshold that was active at the time, even if
  that threshold later moves. The journal is append-only with respect to
  thresholds, never rewritten.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chess_coach import journal as j


@pytest.fixture
def tmp_journal(tmp_path: Path) -> j.Journal:
    return j.open_journal(tmp_path / "test.db")


def test_schema_initializes(tmp_journal: j.Journal) -> None:
    assert tmp_journal.schema_version() == j.SCHEMA_VERSION


def test_schema_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "test.db"
    a = j.open_journal(p)
    b = j.open_journal(p)
    assert a.path == b.path == p


def test_expected_tables_exist(tmp_journal: j.Journal) -> None:
    with tmp_journal.read() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert {
        "games", "positions", "evals", "mistakes",
        "drills", "drill_attempts", "skill_snapshots",
        "coach_notes", "schema_meta",
    } <= names


def test_insert_game_then_position_then_eval_roundtrip(tmp_journal: j.Journal) -> None:
    with tmp_journal.transaction() as conn:
        gid = j.insert_game(
            conn,
            source="lichess",
            external_id="abcd1234",
            played_at="2026-09-17T20:00:00+00:00",
            color="white",
            result="win",
            pgn="[Event 'T'] 1. e4 e5 2. Qh5 *",
            my_rating=1500,
            opp_rating=1480,
        )
        pid = j.insert_position(
            conn, game_id=gid, ply=1, fen=chess_starting_fen(),
            move_san="e4", move_uci="e2e4", clock_ms=600000,
        )
        eid = j.insert_eval(
            conn, position_id=pid, engine="stockfish-19",
            depth=20, cp=35, best_uci="e7e5", pv="e2e4 e7e5",
        )
    with tmp_journal.read() as conn:
        game = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
        pos = conn.execute("SELECT * FROM positions WHERE id = ?", (pid,)).fetchone()
        ev = conn.execute("SELECT * FROM evals WHERE id = ?", (eid,)).fetchone()
    assert game["source"] == "lichess" and game["external_id"] == "abcd1234"
    assert pos["clock_ms"] == 600000
    assert ev["engine"] == "stockfish-19" and ev["depth"] == 20


def test_game_upsert_on_source_external_id(tmp_journal: j.Journal) -> None:
    with tmp_journal.transaction() as conn:
        a = j.insert_game(
            conn, source="lichess", external_id="xyz", played_at="2026-01-01T00:00:00+00:00",
            color="white", result="win", pgn="...",
        )
        b = j.insert_game(
            conn, source="lichess", external_id="xyz", played_at="2026-01-01T00:00:00+00:00",
            color="white", result="win", pgn="...",
        )
    assert a == b


def test_position_upsert_on_game_ply(tmp_journal: j.Journal) -> None:
    with tmp_journal.transaction() as conn:
        gid = j.insert_game(
            conn, source="lichess", external_id=None, played_at="2026-01-01T00:00:00+00:00",
            color="white", result="draw", pgn="...",
        )
        a = j.insert_position(
            conn, game_id=gid, ply=5, fen="startpos",
            move_san=None, move_uci="e2e4", clock_ms=500_000,
        )
        b = j.insert_position(
            conn, game_id=gid, ply=5, fen="startpos",
            move_san=None, move_uci="e2e4", clock_ms=499_500,  # clock ticked down
        )
    assert a == b
    with tmp_journal.read() as conn:
        clock = conn.execute(
            "SELECT clock_ms FROM positions WHERE id = ?", (a,)
        ).fetchone()["clock_ms"]
    assert clock == 499_500


def test_mistake_records_threshold_in_force(tmp_journal: j.Journal) -> None:
    with tmp_journal.transaction() as conn:
        gid = j.insert_game(
            conn, source="lichess", external_id=None, played_at="2026-01-01T00:00:00+00:00",
            color="white", result="loss", pgn="...",
        )
        pid = j.insert_position(
            conn, game_id=gid, ply=20, fen="startpos",
        )
        mid = j.insert_mistake(
            conn, position_id=pid, game_id=gid, delta_cp=-250,
            severity="blunder", classification="tactical", phase="middlegame",
            motifs=["hung_piece"], engine_best_uci="e2e4", played_uci="d1h5",
            instructive=True, threshold_cp_in_force=150,
        )
    with tmp_journal.read() as conn:
        row = conn.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
    assert row["threshold_cp_in_force"] == 150
    assert row["instructive"] == 1
    assert row["rejection_reason"] is None


def test_rejected_mistake_keeps_reason(tmp_journal: j.Journal) -> None:
    with tmp_journal.transaction() as conn:
        gid = j.insert_game(
            conn, source="local", external_id=None, played_at="2026-01-01T00:00:00+00:00",
            color="black", result="loss", pgn="...",
        )
        pid = j.insert_position(
            conn, game_id=gid, ply=12, fen="startpos",
        )
        mid = j.insert_mistake(
            conn, position_id=pid, game_id=gid, delta_cp=-90,
            severity="inaccuracy", classification="positional", phase="endgame",
            motifs=["bad_trade"], engine_best_uci="e2e4", played_uci="d1h5",
            instructive=False, threshold_cp_in_force=150,
            rejection_reason="below_threshold",
        )
    with tmp_journal.read() as conn:
        row = conn.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
        assert row["instructive"] == 0
        assert row["rejection_reason"] == "below_threshold"
        reasons = j.count_rejections_by_reason(conn)
        assert reasons.get("below_threshold") == 1


def test_severity_check_constraint_rejects_unknown(tmp_journal: j.Journal) -> None:
    with tmp_journal.transaction() as conn:
        gid = j.insert_game(
            conn, source="lichess", external_id=None, played_at="2026-01-01T00:00:00+00:00",
            color="white", result="win", pgn="...",
        )
        pid = j.insert_position(conn, game_id=gid, ply=1, fen="startpos")
        # Bad severity: caught at the Python boundary (ValueError) OR by the
        # schema CHECK (IntegrityError). Both are correct rejections; the
        # journal has two layers of defence. Whichever fires, the row must
        # not be written.
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            j.insert_mistake(
                conn, position_id=pid, game_id=gid, delta_cp=-150,
                severity="catastrophe",  # not in the allowed list
                classification="tactical", phase="opening",
                motifs=["x"], engine_best_uci="e2e4", played_uci="d1h5",
                instructive=True, threshold_cp_in_force=100,
            )
    # Confirm nothing leaked past the boundary.
    with tmp_journal.read() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM mistakes").fetchone()["n"]
    assert n == 0


def test_invalid_args_rejected_at_api_boundary(tmp_journal: j.Journal) -> None:
    """Bad inputs should fail loudly at the API, not silently write junk."""
    with tmp_journal.transaction() as conn:
        with pytest.raises(ValueError):
            j.insert_game(
                conn, source="bogus", external_id=None,
                played_at="2026-01-01T00:00:00+00:00",
                color="white", result="win", pgn="...",
            )


def chess_starting_fen() -> str:
    return "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"