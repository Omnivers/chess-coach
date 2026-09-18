"""Phase 0: journal schema, migrations, and thin repository layer.

Truth layer per PROJECT.md §2. No network, no LLM, no clock-during-import
side effects. SQLite + WAL for concurrent reads.

Schema follows PROJECT.md §4 verbatim. The one extension is an auto-increment
`id` on tables the plan didn't number; every primary key is INTEGER PRIMARY KEY
so SQLite gives us a ROWID alias for free.

Two lessons carried over from Coin Scout:
- `threshold_*_in_force` columns store the active threshold with the row,
  never a bare pass/fail. Old rows stay interpretable when the bar moves.
- Rejected candidates get a `rejected` flag rather than disappearing. They are
  the negative class if we ever fit the instructive filter empirically.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence


SCHEMA_VERSION = 3

# How long a connection waits for a write lock before giving up. Every
# transaction here is a handful of small statements, so five seconds is
# far beyond any legitimate wait — it exists so a concurrent writer
# queues instead of erroring out (see `Journal.connect`).
BUSY_TIMEOUT_MS = 5_000

# ORDER MATTERS: tables reference each other. games is the root; positions
# reference games; everything else references positions or mistakes.
SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS games (
        id              INTEGER PRIMARY KEY,
        source          TEXT NOT NULL CHECK (source IN ('lichess','chesscom','local','otb')),
        external_id     TEXT,
        played_at       TEXT NOT NULL,
        color           TEXT NOT NULL CHECK (color IN ('white','black')),
        result          TEXT NOT NULL CHECK (result IN ('win','loss','draw','*')),
        time_control    TEXT,
        my_rating       INTEGER,
        opp_rating      INTEGER,
        eco             TEXT,
        opening_name    TEXT,
        pgn             TEXT NOT NULL,
        UNIQUE (source, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_games_played_at ON games(played_at)",
    "CREATE INDEX IF NOT EXISTS idx_games_source_ext ON games(source, external_id)",
    """
    CREATE TABLE IF NOT EXISTS positions (
        id              INTEGER PRIMARY KEY,
        game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        ply             INTEGER NOT NULL,
        fen             TEXT NOT NULL,
        move_san        TEXT,
        move_uci        TEXT,
        clock_ms        INTEGER,
        UNIQUE (game_id, ply)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_positions_game ON positions(game_id)",
    """
    CREATE TABLE IF NOT EXISTS evals (
        id              INTEGER PRIMARY KEY,
        position_id     INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
        engine          TEXT NOT NULL,
        depth           INTEGER NOT NULL,
        nodes           INTEGER,
        cp              INTEGER,
        mate            INTEGER,
        best_uci        TEXT,
        pv              TEXT,
        multipv_rank    INTEGER NOT NULL DEFAULT 1,
        evaluated_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_evals_position ON evals(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_evals_position_rank ON evals(position_id, multipv_rank)",
    """
    CREATE TABLE IF NOT EXISTS mistakes (
        id                          INTEGER PRIMARY KEY,
        position_id                 INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
        game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        delta_cp                    INTEGER NOT NULL,
        severity                    TEXT NOT NULL CHECK (severity IN ('inaccuracy','mistake','blunder')),
        class                       TEXT NOT NULL CHECK (class IN ('tactical','positional','opening','endgame','time')),
        phase                       TEXT NOT NULL CHECK (phase IN ('opening','middlegame','endgame')),
        motifs                      TEXT NOT NULL DEFAULT '',  -- comma-separated, validated at write time
        engine_best_uci             TEXT NOT NULL,
        played_uci                  TEXT NOT NULL,
        instructive                 INTEGER NOT NULL CHECK (instructive IN (0,1)),
        threshold_cp_in_force       INTEGER NOT NULL,
        rejection_reason            TEXT,                     -- null when instructive=1
        explanation                 TEXT,
        explained_at                TEXT,
        created_at                  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mistakes_game ON mistakes(game_id)",
    "CREATE INDEX IF NOT EXISTS idx_mistakes_class ON mistakes(class)",
    "CREATE INDEX IF NOT EXISTS idx_mistakes_instructive ON mistakes(instructive)",
    """
    CREATE TABLE IF NOT EXISTS drills (
        id              INTEGER PRIMARY KEY,
        mistake_id      INTEGER NOT NULL REFERENCES mistakes(id) ON DELETE CASCADE,
        fen             TEXT NOT NULL,
        solution_uci    TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        due_at          TEXT NOT NULL,
        interval_days   REAL NOT NULL DEFAULT 1.0,
        ease            REAL NOT NULL DEFAULT 2.5,
        reps            INTEGER NOT NULL DEFAULT 0,
        lapses          INTEGER NOT NULL DEFAULT 0,
        retired         INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0,1))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_drills_due ON drills(due_at) WHERE retired = 0",
    """
    CREATE TABLE IF NOT EXISTS drill_attempts (
        id              INTEGER PRIMARY KEY,
        drill_id        INTEGER NOT NULL REFERENCES drills(id) ON DELETE CASCADE,
        attempted_at    TEXT NOT NULL,
        correct         INTEGER NOT NULL CHECK (correct IN (0,1)),
        time_ms         INTEGER,
        moved_uci       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempts_drill ON drill_attempts(drill_id)",
    """
    CREATE TABLE IF NOT EXISTS skill_snapshots (
        id              INTEGER PRIMARY KEY,
        taken_at        TEXT NOT NULL,
        metric          TEXT NOT NULL,
        value           REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snapshots_metric_time ON skill_snapshots(metric, taken_at)",
    """
    CREATE TABLE IF NOT EXISTS coach_notes (
        id              INTEGER PRIMARY KEY,
        created_at      TEXT NOT NULL,
        scope           TEXT NOT NULL,
        note            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notes_scope_time ON coach_notes(scope, created_at)",
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key             TEXT PRIMARY KEY,
        value           TEXT NOT NULL
    )
    """,
    # -------------------------------------------------------- schema v3
    """
    CREATE TABLE IF NOT EXISTS highlights (
        id            INTEGER PRIMARY KEY,
        position_id   INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
        game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN
                        ('only_move','found_tactic','resisted','converted','best_under_pressure')),
        delta_to_2nd  INTEGER,
        ply           INTEGER NOT NULL,
        played_uci    TEXT NOT NULL,
        note          TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_highlights_game ON highlights(game_id)",
    """
    CREATE TABLE IF NOT EXISTS hint_events (
        id            INTEGER PRIMARY KEY,
        game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        ply           INTEGER NOT NULL,
        tier          INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 4),
        motif         TEXT,
        credits_left  INTEGER NOT NULL,
        requested_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hint_events_game ON hint_events(game_id)",
    """
    CREATE TABLE IF NOT EXISTS guard_events (
        id            INTEGER PRIMARY KEY,
        game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        ply           INTEGER NOT NULL,
        intended_uci  TEXT NOT NULL,
        delta_cp      INTEGER NOT NULL,
        overridden    INTEGER NOT NULL DEFAULT 0 CHECK (overridden IN (0,1)),
        fired_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_guard_events_game ON guard_events(game_id)",
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id            INTEGER PRIMARY KEY,
        day           TEXT NOT NULL UNIQUE,
        game_id       INTEGER REFERENCES games(id) ON DELETE SET NULL,
        drills_done   INTEGER NOT NULL DEFAULT 0,
        drills_total  INTEGER NOT NULL DEFAULT 0,
        reviewed      INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0,1)),
        started_at    TEXT NOT NULL,
        completed_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_state (
        game_id       INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
        external_id   TEXT NOT NULL UNIQUE,
        user_color    TEXT NOT NULL CHECK (user_color IN ('white','black')),
        engine_elo    INTEGER NOT NULL,
        fen           TEXT NOT NULL,
        moves_san     TEXT NOT NULL DEFAULT '',
        white_ms      INTEGER,
        black_ms      INTEGER,
        increment_ms  INTEGER NOT NULL DEFAULT 0,
        hint_credits  INTEGER NOT NULL DEFAULT 6,
        terminated    INTEGER NOT NULL DEFAULT 0 CHECK (terminated IN (0,1)),
        result        TEXT,
        updated_at    TEXT NOT NULL
    )
    """,
)


VALID_SEVERITY = ("inaccuracy", "mistake", "blunder")
VALID_CLASS = ("tactical", "positional", "opening", "endgame", "time")
VALID_PHASE = ("opening", "middlegame", "endgame")
VALID_COLOR = ("white", "black")
VALID_RESULT = ("win", "loss", "draw", "*")
VALID_SOURCE = ("lichess", "chesscom", "local", "otb")


@dataclass(frozen=True)
class Journal:
    """A journal is a single SQLite database file with the chess_coach schema.

    Connection-per-call is fine for Phase 0 (CLI only). FastAPI later will
    hold a process-wide engine and use connection-per-request.
    """

    path: Path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        # WAL lets readers and one writer run concurrently, but a SECOND
        # writer still blocks — and SQLite's default busy timeout is 0, so
        # it raises "database is locked" instantly rather than waiting.
        # That is reachable in normal use: the server writes a move while
        # an offline analysis run (scripts/reanalyze.py) writes a mistake.
        # Waiting is the right answer for both — these transactions are
        # milliseconds long, so the timeout is a safety net, not a stall.
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Idempotent. Safe to call on every open. Runs pending migrations.

        Migration 1 → 2: the result CHECK constraint now allows '*' for
        in-progress games (live Phase 5 play). SQLite can't ALTER a
        CHECK, so we rename the old games table, recreate it with the
        updated constraint, and copy data over. Dependent tables
        (mistakes, positions, evals) all FK to games, so we have to
        recreate them too. The cascade would otherwise strand foreign
        keys pointing to the renamed-then-dropped table.
        """
        # CREATE TABLE IF NOT EXISTS first — fresh databases need them,
        # migrated databases already have them.
        with self.transaction() as conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
            current = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_version = int(current["value"]) if current else 0
            if current_version < 2 and current_version >= 1:
                # The v3 tables above (highlights, hint_events,
                # guard_events, sessions, live_state) FK to games, and
                # highlights also FKs to positions. They were just
                # created fresh by the loop above — a v1 database can't
                # have had them before, so they're guaranteed empty.
                # SQLite auto-rewrites a referencing table's stored FK
                # text when the table it points to is renamed (see the
                # ALTER TABLE RENAME calls below), which corrupts these
                # five once games/positions get renamed out from under
                # them and leaves the DROP TABLE dance below unable to
                # find the renamed table it expects. Drop them now
                # (no data lost — they can't have any yet) and let the
                # SCHEMA loop recreate them after the rename dance
                # finishes, pointed at the rebuilt games/positions.
                for _v3_table in (
                    "highlights", "hint_events", "guard_events",
                    "sessions", "live_state",
                ):
                    conn.execute(f"DROP TABLE IF EXISTS {_v3_table}")
                # Migrate: rename everything → rebuild → copy → drop old.
                conn.executescript("""
                    ALTER TABLE games RENAME TO games__v1;
                    ALTER TABLE positions RENAME TO positions__v1;
                    ALTER TABLE evals RENAME TO evals__v1;
                    ALTER TABLE mistakes RENAME TO mistakes__v1;
                    CREATE TABLE games (
                        id              INTEGER PRIMARY KEY,
                        source          TEXT NOT NULL CHECK (source IN ('lichess','chesscom','local','otb')),
                        external_id     TEXT,
                        played_at       TEXT NOT NULL,
                        color           TEXT NOT NULL CHECK (color IN ('white','black')),
                        result          TEXT NOT NULL CHECK (result IN ('win','loss','draw','*')),
                        time_control    TEXT,
                        my_rating       INTEGER,
                        opp_rating      INTEGER,
                        eco             TEXT,
                        opening_name    TEXT,
                        pgn             TEXT NOT NULL,
                        UNIQUE (source, external_id)
                    );
                    INSERT INTO games SELECT * FROM games__v1;
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
                    INSERT INTO positions SELECT * FROM positions__v1;
                    CREATE TABLE evals (
                        id              INTEGER PRIMARY KEY,
                        position_id     INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
                        engine          TEXT NOT NULL,
                        depth           INTEGER NOT NULL,
                        nodes           INTEGER,
                        cp              INTEGER,
                        mate            INTEGER,
                        best_uci        TEXT,
                        pv              TEXT,
                        multipv_rank    INTEGER NOT NULL DEFAULT 1,
                        evaluated_at    TEXT NOT NULL
                    );
                    INSERT INTO evals SELECT * FROM evals__v1;
                    CREATE TABLE mistakes (
                        id                          INTEGER PRIMARY KEY,
                        position_id                 INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
                        game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                        delta_cp                    INTEGER NOT NULL,
                        severity                    TEXT NOT NULL CHECK (severity IN ('inaccuracy','mistake','blunder')),
                        class                       TEXT NOT NULL CHECK (class IN ('tactical','positional','opening','endgame','time')),
                        phase                       TEXT NOT NULL CHECK (phase IN ('opening','middlegame','endgame')),
                        motifs                      TEXT NOT NULL DEFAULT '',
                        engine_best_uci             TEXT NOT NULL,
                        played_uci                  TEXT NOT NULL,
                        instructive                 INTEGER NOT NULL CHECK (instructive IN (0,1)),
                        threshold_cp_in_force       INTEGER NOT NULL,
                        rejection_reason            TEXT,
                        explanation                 TEXT,
                        explained_at                TEXT,
                        created_at                  TEXT NOT NULL
                    );
                    INSERT INTO mistakes SELECT * FROM mistakes__v1;
                    DROP TABLE mistakes__v1;
                    DROP TABLE evals__v1;
                    DROP TABLE positions__v1;
                    DROP TABLE games__v1;
                    -- Re-create the indexes that the original schema had.
                    CREATE INDEX IF NOT EXISTS idx_games_played_at ON games(played_at);
                    CREATE INDEX IF NOT EXISTS idx_games_source_ext ON games(source, external_id);
                    CREATE INDEX IF NOT EXISTS idx_positions_game ON positions(game_id);
                    CREATE INDEX IF NOT EXISTS idx_evals_position ON evals(position_id);
                    CREATE INDEX IF NOT EXISTS idx_evals_position_rank ON evals(position_id, multipv_rank);
                    CREATE INDEX IF NOT EXISTS idx_mistakes_game ON mistakes(game_id);
                    CREATE INDEX IF NOT EXISTS idx_mistakes_class ON mistakes(class);
                    CREATE INDEX IF NOT EXISTS idx_mistakes_instructive ON mistakes(instructive);
                """)
                # Recreate the five v3 tables dropped above, now pointed
                # at the rebuilt games/positions tables.
                for stmt in SCHEMA:
                    conn.execute(stmt)
            # Schema v3: `out_of_book_ply` on games. SQLite has no
            # `ADD COLUMN IF NOT EXISTS`, so guard with PRAGMA table_info.
            # This must run AFTER the v1->v2 rebuild above — that rebuild
            # recreates `games` from a hardcoded CREATE TABLE without this
            # column, so adding it before the rebuild would just have it
            # dropped again.
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(games)")
            }
            if "out_of_book_ply" not in existing_cols:
                conn.execute("ALTER TABLE games ADD COLUMN out_of_book_ply INTEGER")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def schema_version(self) -> Optional[int]:
        with self.read() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:
            return None
        return int(row["value"])


def utc_now_iso() -> str:
    """ISO-8601 in UTC, second precision. Stable across calls."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(ts: str) -> datetime:
    """Parse the timestamp format produced by utc_now_iso. Tolerates trailing Z."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def insert_game(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: Optional[str],
    played_at: str,
    color: str,
    result: str,
    pgn: str,
    time_control: Optional[str] = None,
    my_rating: Optional[int] = None,
    opp_rating: Optional[int] = None,
    eco: Optional[str] = None,
    opening_name: Optional[str] = None,
) -> int:
    """Insert a game and return its id.

    (source, external_id) is UNIQUE; on conflict we return the existing row's id
    so re-running an import doesn't duplicate games. This is the idempotency
    contract Phase 1's `since=<ms>` incremental ingest will rely on.
    """
    if source not in VALID_SOURCE:
        raise ValueError(f"invalid source: {source}")
    if color not in VALID_COLOR:
        raise ValueError(f"invalid color: {color}")
    if result not in VALID_RESULT:
        raise ValueError(f"invalid result: {result}")
    row = conn.execute(
        "SELECT id FROM games WHERE source = ? AND external_id IS ?",
        (source, external_id),
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        """
        INSERT INTO games(
            source, external_id, played_at, color, result,
            time_control, my_rating, opp_rating, eco, opening_name, pgn
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source, external_id, played_at, color, result,
            time_control, my_rating, opp_rating, eco, opening_name, pgn,
        ),
    )
    return int(cur.lastrowid)


def insert_position(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    ply: int,
    fen: str,
    move_san: Optional[str] = None,
    move_uci: Optional[str] = None,
    clock_ms: Optional[int] = None,
) -> int:
    """Insert a position. (game_id, ply) is UNIQUE — replays upsert."""
    row = conn.execute(
        "SELECT id FROM positions WHERE game_id = ? AND ply = ?",
        (game_id, ply),
    ).fetchone()
    if row is not None:
        conn.execute(
            """
            UPDATE positions
               SET fen = ?, move_san = ?, move_uci = ?, clock_ms = ?
             WHERE id = ?
            """,
            (fen, move_san, move_uci, clock_ms, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        """
        INSERT INTO positions(game_id, ply, fen, move_san, move_uci, clock_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_id, ply, fen, move_san, move_uci, clock_ms),
    )
    return int(cur.lastrowid)


def insert_eval(
    conn: sqlite3.Connection,
    *,
    position_id: int,
    engine: str,
    depth: int,
    cp: Optional[int],
    best_uci: Optional[str],
    pv: Optional[str],
    multipv_rank: int = 1,
    mate: Optional[int] = None,
    nodes: Optional[int] = None,
) -> int:
    """Insert a Stockfish evaluation for a position.

    Multiple multipv ranks per position are allowed (1 = best move, 2 = second,
    etc.) so the UI can show "what else I had." No UNIQUE constraint: re-runs
    of the same position at higher depth accumulate as separate rows so we
    don't lose shallower history.
    """
    cur = conn.execute(
        """
        INSERT INTO evals(
            position_id, engine, depth, nodes, cp, mate,
            best_uci, pv, multipv_rank, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position_id, engine, depth, nodes, cp, mate,
            best_uci, pv, multipv_rank, utc_now_iso(),
        ),
    )
    return int(cur.lastrowid)


def insert_mistake(
    conn: sqlite3.Connection,
    *,
    position_id: int,
    game_id: int,
    delta_cp: int,
    severity: str,
    classification: str,
    phase: str,
    motifs: Sequence[str] | str,
    engine_best_uci: str,
    played_uci: str,
    instructive: bool,
    threshold_cp_in_force: int,
    rejection_reason: Optional[str] = None,
    explanation: Optional[str] = None,
) -> int:
    """Insert a detected mistake (or rejection row, when instructive=False).

    `motifs` may be a sequence of strings or a comma-separated string. It is
    normalised to comma-separated and validated non-empty. A rejected row
    (instructive=False) is expected to carry a rejection_reason; the schema
    doesn't enforce it because we're already trusting the writer, but tests
    should assert it.
    """
    if severity not in VALID_SEVERITY:
        raise ValueError(f"invalid severity: {severity}")
    if classification not in VALID_CLASS:
        raise ValueError(f"invalid class: {classification}")
    if phase not in VALID_PHASE:
        raise ValueError(f"invalid phase: {phase}")
    if isinstance(motifs, str):
        motif_list = [m for m in motifs.split(",") if m.strip()]
    else:
        motif_list = [m for m in motifs if m.strip()]
    # Empty motifs are allowed: they mean "the detector found a real
    # mistake/inaccuracy but no deterministic motif attached." Phase 2
    # narration will fill the gap. The row is still useful for the
    # instructive filter and the journal.
    motifs_csv = ",".join(motif_list)
    cur = conn.execute(
        """
        INSERT INTO mistakes(
            position_id, game_id, delta_cp, severity, class, phase, motifs,
            engine_best_uci, played_uci, instructive, threshold_cp_in_force,
            rejection_reason, explanation, explained_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position_id, game_id, delta_cp, severity, classification, phase, motifs_csv,
            engine_best_uci, played_uci, 1 if instructive else 0,
            threshold_cp_in_force,
            rejection_reason,
            explanation,
            utc_now_iso() if explanation is not None else None,
            utc_now_iso(),
        ),
    )
    return int(cur.lastrowid)


def list_instructive_mistakes(
    conn: sqlite3.Connection, *, limit: int = 100
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM mistakes
         WHERE instructive = 1
         ORDER BY created_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def count_rejections_by_reason(conn: sqlite3.Connection) -> dict[str, int]:
    """Negative-class audit. Always present, even when empty."""
    rows = conn.execute(
        """
        SELECT COALESCE(rejection_reason, '(unset)') AS reason, COUNT(*) AS n
          FROM mistakes
         WHERE instructive = 0
         GROUP BY reason
         ORDER BY n DESC
        """
    ).fetchall()
    return {r["reason"]: r["n"] for r in rows}


_VALID_HIGHLIGHT_KIND = (
    "only_move", "found_tactic", "resisted", "converted", "best_under_pressure",
)

# Columns `save_live_state` is allowed to upsert via **fields, beyond the
# game_id/external_id it always takes positionally. Whitelisted so a typo'd
# kwarg fails loudly instead of being silently dropped or SQL-injected.
LIVE_STATE_FIELDS = (
    "user_color", "engine_elo", "fen", "moves_san", "white_ms", "black_ms",
    "increment_ms", "hint_credits", "terminated", "result", "updated_at",
)


def insert_highlight(
    conn: sqlite3.Connection,
    *,
    position_id: int,
    game_id: int,
    kind: str,
    ply: int,
    played_uci: str,
    delta_to_2nd: Optional[int] = None,
    note: Optional[str] = None,
) -> int:
    """Record a positive-class moment (ROADMAP §3.2). See highlights.py."""
    if kind not in _VALID_HIGHLIGHT_KIND:
        raise ValueError(f"invalid highlight kind: {kind}")
    cur = conn.execute(
        """
        INSERT INTO highlights(
            position_id, game_id, kind, delta_to_2nd, ply, played_uci, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (position_id, game_id, kind, delta_to_2nd, ply, played_uci, note, utc_now_iso()),
    )
    return int(cur.lastrowid)


def list_highlights(conn: sqlite3.Connection, *, game_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM highlights WHERE game_id = ? ORDER BY ply ASC",
        (game_id,),
    ).fetchall()


def insert_hint_event(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    ply: int,
    tier: int,
    credits_left: int,
    motif: Optional[str] = None,
) -> int:
    """Record one rung of the hint ladder being taken (ROADMAP §2.5)."""
    cur = conn.execute(
        """
        INSERT INTO hint_events(game_id, ply, tier, motif, credits_left, requested_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_id, ply, tier, motif, credits_left, utc_now_iso()),
    )
    return int(cur.lastrowid)


def insert_guard_event(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    ply: int,
    intended_uci: str,
    delta_cp: int,
    overridden: bool = False,
) -> int:
    """Record the blunder guard firing (ROADMAP §2.2)."""
    cur = conn.execute(
        """
        INSERT INTO guard_events(game_id, ply, intended_uci, delta_cp, overridden, fired_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_id, ply, intended_uci, delta_cp, 1 if overridden else 0, utc_now_iso()),
    )
    return int(cur.lastrowid)


def insert_drill(
    conn: sqlite3.Connection,
    *,
    mistake_id: int,
    fen: str,
    solution_uci: str,
    due_at: Optional[str] = None,
) -> int:
    """Insert a drill for a mistake, or return the existing one.

    Idempotent per `mistake_id`: a mistake should only ever spawn one
    live drill. If a non-retired drill already exists for it, that id
    is returned instead of inserting a duplicate — re-running analysis
    on the same game must not flood the queue.
    """
    row = conn.execute(
        "SELECT id FROM drills WHERE mistake_id = ? AND retired = 0",
        (mistake_id,),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO drills(mistake_id, fen, solution_uci, created_at, due_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (mistake_id, fen, solution_uci, now, due_at if due_at is not None else now),
    )
    return int(cur.lastrowid)


def list_due_drills(
    conn: sqlite3.Connection, *, now_iso: str, limit: int = 8
) -> list[sqlite3.Row]:
    """Drills ready to review: not retired, due at or before `now_iso`.

    Joined to mistakes (for motifs/classification) and games (for
    external_id), as `GET /drills/due` needs both.
    """
    return conn.execute(
        """
        SELECT
            drills.*,
            mistakes.motifs      AS motifs,
            mistakes.class       AS class,
            mistakes.severity    AS severity,
            games.external_id    AS external_id,
            positions.ply        AS ply
        FROM drills
        JOIN mistakes  ON mistakes.id = drills.mistake_id
        JOIN games     ON games.id = mistakes.game_id
        JOIN positions ON positions.id = mistakes.position_id
        WHERE drills.retired = 0 AND drills.due_at <= ?
        ORDER BY drills.due_at ASC
        LIMIT ?
        """,
        (now_iso, limit),
    ).fetchall()


def insert_drill_attempt(
    conn: sqlite3.Connection,
    *,
    drill_id: int,
    correct: bool,
    time_ms: Optional[int],
    moved_uci: Optional[str],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO drill_attempts(drill_id, attempted_at, correct, time_ms, moved_uci)
        VALUES (?, ?, ?, ?, ?)
        """,
        (drill_id, utc_now_iso(), 1 if correct else 0, time_ms, moved_uci),
    )
    return int(cur.lastrowid)


def update_drill_schedule(
    conn: sqlite3.Connection,
    *,
    drill_id: int,
    due_at: str,
    interval_days: float,
    ease: float,
    reps: int,
    lapses: int,
    retired: bool,
) -> None:
    """Write the next `srs.Schedule` back onto a drill row."""
    conn.execute(
        """
        UPDATE drills
           SET due_at = ?, interval_days = ?, ease = ?, reps = ?, lapses = ?, retired = ?
         WHERE id = ?
        """,
        (due_at, interval_days, ease, reps, lapses, 1 if retired else 0, drill_id),
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    *,
    metric: str,
    value: float,
    taken_at: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO skill_snapshots(taken_at, metric, value) VALUES (?, ?, ?)",
        (taken_at if taken_at is not None else utc_now_iso(), metric, value),
    )
    return int(cur.lastrowid)


def get_or_create_session(conn: sqlite3.Connection, *, day: str) -> sqlite3.Row:
    """Fetch today's session row, creating it (empty) if this is the first touch."""
    row = conn.execute("SELECT * FROM sessions WHERE day = ?", (day,)).fetchone()
    if row is not None:
        return row
    conn.execute(
        "INSERT INTO sessions(day, started_at) VALUES (?, ?)",
        (day, utc_now_iso()),
    )
    return conn.execute("SELECT * FROM sessions WHERE day = ?", (day,)).fetchone()


def update_session(conn: sqlite3.Connection, *, day: str, **fields) -> None:
    """Merge `fields` into today's session row. Creates the row first if needed."""
    get_or_create_session(conn, day=day)
    if not fields:
        return
    allowed = {"game_id", "drills_done", "drills_total", "reviewed", "completed_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown session field(s): {sorted(unknown)}")
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE sessions SET {set_clause} WHERE day = ?",
        (*fields.values(), day),
    )


def session_streak(conn: sqlite3.Connection, *, today: str) -> int:
    """Consecutive days ending today (or yesterday) with a sessions row.

    A streak survives one day of grace: if `today` has no row yet (the
    user just hasn't opened the app yet today) but yesterday does, the
    streak still counts up to yesterday. Two missed days in a row breaks
    it.
    """
    from datetime import timedelta

    rows = conn.execute("SELECT day FROM sessions ORDER BY day DESC").fetchall()
    days = {r["day"] for r in rows}
    if not days:
        return 0
    cursor = parse_iso(today + "T00:00:00+00:00").date()
    if today not in days:
        cursor -= timedelta(days=1)
        if cursor.isoformat() not in days:
            return 0
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def save_live_state(
    conn: sqlite3.Connection, *, game_id: int, external_id: str, **fields
) -> None:
    """Upsert the resumable state of a live game (ROADMAP §1.2).

    `game_id` is the primary key. Unknown fields are the same programmer
    error as a bad column name — raise, don't drop silently.

    `updated_at` is accepted but optional: callers that already computed
    "now" once for a batch of writes (e.g. api_play.py, so every table
    touched by one move shares an identical timestamp) may pass it
    explicitly; otherwise it's stamped here via `utc_now_iso()`.
    """
    unknown = set(fields) - set(LIVE_STATE_FIELDS)
    if unknown:
        raise ValueError(f"unknown live_state field(s): {sorted(unknown)}")
    updated_at = fields.pop("updated_at", None) or utc_now_iso()
    defaults = {
        "user_color": "white",
        "engine_elo": 1500,
        "fen": chess_starting_fen(),
        "moves_san": "",
        "white_ms": None,
        "black_ms": None,
        "increment_ms": 0,
        "hint_credits": 6,
        "terminated": 0,
        "result": None,
    }
    existing = conn.execute(
        "SELECT * FROM live_state WHERE game_id = ?", (game_id,)
    ).fetchone()
    merged = dict(defaults)
    if existing is not None:
        merged.update({k: existing[k] for k in defaults})
    merged.update(fields)
    if "terminated" in fields:
        merged["terminated"] = 1 if fields["terminated"] else 0
    conn.execute(
        """
        INSERT INTO live_state(
            game_id, external_id, user_color, engine_elo, fen, moves_san,
            white_ms, black_ms, increment_ms, hint_credits, terminated,
            result, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            external_id  = excluded.external_id,
            user_color   = excluded.user_color,
            engine_elo   = excluded.engine_elo,
            fen          = excluded.fen,
            moves_san    = excluded.moves_san,
            white_ms     = excluded.white_ms,
            black_ms     = excluded.black_ms,
            increment_ms = excluded.increment_ms,
            hint_credits = excluded.hint_credits,
            terminated   = excluded.terminated,
            result       = excluded.result,
            updated_at   = excluded.updated_at
        """,
        (
            game_id, external_id, merged["user_color"], merged["engine_elo"],
            merged["fen"], merged["moves_san"], merged["white_ms"],
            merged["black_ms"], merged["increment_ms"], merged["hint_credits"],
            merged["terminated"], merged["result"], updated_at,
        ),
    )


def load_live_state(conn: sqlite3.Connection, *, external_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM live_state WHERE external_id = ?", (external_id,)
    ).fetchone()


def sweep_stale_live_games(conn: sqlite3.Connection, *, older_than_iso: str) -> int:
    """Close out abandoned live games so they stop showing as '*' forever.

    Any game with result='*' whose live_state hasn't been touched since
    before `older_than_iso` gets closed as a draw (we truly don't know
    the result — a disconnect isn't a loss) and its live_state row is
    dropped. Safe on an empty database: the SELECT returns no rows and
    the function returns 0.
    """
    stale = conn.execute(
        """
        SELECT games.id AS game_id
          FROM live_state
          JOIN games ON games.id = live_state.game_id
         WHERE games.result = '*' AND live_state.updated_at < ?
        """,
        (older_than_iso,),
    ).fetchall()
    for row in stale:
        conn.execute(
            "UPDATE games SET result = 'draw' WHERE id = ?", (row["game_id"],)
        )
        conn.execute(
            "DELETE FROM live_state WHERE game_id = ?", (row["game_id"],)
        )
    return len(stale)


def chess_starting_fen() -> str:
    """The standard chess starting position FEN.

    Kept here (rather than importing python-chess) so this module has no
    dependency beyond the standard library — `journal.py` is the data
    layer and truth-layer facts like "what's the starting FEN" belong to
    `chess.Board().fen()`, but this one constant never changes, so a
    literal avoids an otherwise-unneeded import.
    """
    return "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def open_journal(path: str | Path) -> Journal:
    """Convenience: build a Journal, ensure parent dir, initialize schema."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    j = Journal(p)
    j.initialize()
    return j


__all__ = [
    "Journal",
    "SCHEMA_VERSION",
    "VALID_SEVERITY",
    "VALID_CLASS",
    "VALID_PHASE",
    "VALID_COLOR",
    "VALID_RESULT",
    "VALID_SOURCE",
    "utc_now_iso",
    "parse_iso",
    "open_journal",
    "insert_game",
    "insert_position",
    "insert_eval",
    "insert_mistake",
    "list_instructive_mistakes",
    "count_rejections_by_reason",
    "LIVE_STATE_FIELDS",
    "insert_highlight",
    "list_highlights",
    "insert_hint_event",
    "insert_guard_event",
    "insert_drill",
    "list_due_drills",
    "insert_drill_attempt",
    "update_drill_schedule",
    "insert_snapshot",
    "get_or_create_session",
    "update_session",
    "session_streak",
    "save_live_state",
    "load_live_state",
    "sweep_stale_live_games",
    "chess_starting_fen",
]