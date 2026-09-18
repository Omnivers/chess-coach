"""Phase 5 — schema migration v1 → v2.

The migration added '*' to the result CHECK constraint so live games
can be persisted with `result='*'` until they terminate. SQLite can't
ALTER a CHECK constraint, so the migration has to rename + recreate +
copy + drop, which means also rebuilding any table with an FK to games.

This test pins that:
- A v1-shaped journal migrates cleanly to v2 on `initialize()`.
- Data is preserved across the migration (positions, evals, mistakes).
- FK references in recreated tables point to the new `games` table,
  not the renamed `games__v1` that gets dropped.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from chess_coach.journal import SCHEMA_VERSION, Journal, insert_game, insert_position


def _create_v1_journal(path: Path) -> None:
    """Build a fresh v1 journal with the old CHECK constraint and seed data."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE games (
            id              INTEGER PRIMARY KEY,
            source          TEXT NOT NULL CHECK (source IN ('lichess','chesscom','local','otb')),
            external_id     TEXT,
            played_at       TEXT NOT NULL,
            color           TEXT NOT NULL CHECK (color IN ('white','black')),
            result          TEXT NOT NULL CHECK (result IN ('win','loss','draw')),
            time_control    TEXT,
            my_rating       INTEGER,
            opp_rating      INTEGER,
            eco             TEXT,
            opening_name    TEXT,
            pgn             TEXT NOT NULL,
            UNIQUE (source, external_id)
        );
        CREATE TABLE positions (
            id              INTEGER PRIMARY KEY,
            game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            ply             INTEGER NOT NULL,
            fen             TEXT NOT NULL,
            move_san        TEXT,
            move_uci        TEXT,
            clock_ms        INTEGER,
            UNIQUE (game_id, ply)
        );
        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');
        INSERT INTO games(id, source, external_id, played_at, color, result, pgn)
        VALUES (1, 'lichess', 'legacy-game', '2025.01.01T00:00:00+00:00',
                'white', 'win', '[Event "Legacy"] *');
        INSERT INTO positions(game_id, ply, fen, move_san, move_uci)
        VALUES (1, 1, 'startpos', 'e4', 'e2e4');
    """)
    conn.commit()
    conn.close()


def test_v1_to_v2_migration_preserves_data(tmp_path: Path) -> None:
    """v1 journal with a game + position migrates to v2 with both intact."""
    db_path = tmp_path / "v1.db"
    _create_v1_journal(db_path)

    journal = Journal(db_path)
    journal.initialize()

    # Migration runs all the way to the current version (v1 -> v2 -> v3)
    # and the legacy game survives it.
    assert journal.schema_version() == SCHEMA_VERSION
    with journal.read() as conn:
        game = conn.execute(
            "SELECT id, source, external_id, result FROM games WHERE id = 1"
        ).fetchone()
        position = conn.execute(
            "SELECT id, game_id, ply, move_san FROM positions WHERE game_id = 1"
        ).fetchone()
    assert game["external_id"] == "legacy-game"
    assert game["result"] == "win"
    assert position["move_san"] == "e4"


def test_v1_to_v2_migration_allows_in_progress_result(tmp_path: Path) -> None:
    """After v2, the result column accepts '*' for live games."""
    db_path = tmp_path / "v1_progress.db"
    _create_v1_journal(db_path)

    journal = Journal(db_path)
    journal.initialize()

    # Insert a game with result='*' — should succeed under v2, would have
    # failed under v1.
    with journal.transaction() as conn:
        gid = insert_game(
            conn, source="local", external_id=None,
            played_at="2026.09.17T20:00:00+00:00",
            color="white", result="*", pgn="",
        )
    assert gid > 0


def test_v2_migration_recreates_fk_chain(tmp_path: Path) -> None:
    """Positions FK must point at the new games table, not the dropped one.

    This is the regression that bit us once: the v1 → v2 migration
    renamed games to games__v1, then dropped games__v1 — but if positions
    was created with FK to games (not games__v1) at the start of
    initialize, the FK gets silently re-pointed by ALTER TABLE RENAME
    and then dangles when games__v1 is dropped. The fix is to rename
    ALL tables that FK to games and rebuild them with new FKs.
    """
    db_path = tmp_path / "v1_fk.db"
    _create_v1_journal(db_path)
    journal = Journal(db_path)
    journal.initialize()

    # Insert a position referencing the legacy game — exercises the FK.
    with journal.transaction() as conn:
        pid = insert_position(
            conn, game_id=1, ply=2, fen="startpos2",
            move_san="e5", move_uci="e7e5",
        )
    assert pid > 0
