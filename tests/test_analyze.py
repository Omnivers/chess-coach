"""Phase 1 — analyse_game end-to-end on a real PGN fixture.

Uses the Morphy Opera Game as the test bed. The game is well-known,
short, and the journal should end up with at least the positions for
every ply plus evals. We do NOT assert on specific mistake counts —
those depend on the user's rating, the engine version, and depth, and
any test that hardcoded them would rot on the first Stockfish upgrade.
"""
from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import chess
import pytest

from chess_coach import analyze as analyze_mod
from chess_coach.analyze import analyse_game
from chess_coach.engine import EngineOptions, StockfishEngine
from chess_coach.journal import Journal, open_journal


FIXTURE_PATH = Path("data/fixtures/morphy_opera.pgn")


@pytest.fixture(scope="module")
def fixture_pgn() -> str:
    if not FIXTURE_PATH.exists():
        pytest.skip(f"fixture not found: {FIXTURE_PATH}")
    return FIXTURE_PATH.read_text()


@pytest.fixture(scope="module")
def engine() -> StockfishEngine:
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    return StockfishEngine(options=EngineOptions(threads=1, hash_mb=32, multipv=3))


@pytest.fixture
def tmp_journal(tmp_path: Path) -> Journal:
    return open_journal(tmp_path / "analyse.db")


def test_analyse_writes_game_row(tmp_journal, engine, fixture_pgn) -> None:
    summary = analyse_game(
        fixture_pgn,
        journal=tmp_journal,
        engine=engine,
        external_id="morphy-opera",
        depth=6,  # shallow depth keeps the test suite fast
    )
    assert summary.game_id > 0
    with tmp_journal.read() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM games").fetchone()["n"]
    assert n == 1


def test_analyse_writes_position_per_ply(tmp_journal, engine, fixture_pgn) -> None:
    summary = analyse_game(
        fixture_pgn,
        journal=tmp_journal,
        engine=engine,
        external_id="morphy-opera-positions",
        depth=6,
    )
    # The Opera Game has 33 half-moves → 33 positions (one per ply, the
    # pre-move state). Each position has 3 multipv evals.
    assert summary.total_positions == 33
    with tmp_journal.read() as conn:
        positions = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
        evals = conn.execute("SELECT COUNT(*) AS n FROM evals").fetchone()["n"]
    assert positions == 33
    assert evals >= 33  # at least one multipv rank per position


def test_analyse_is_idempotent(tmp_journal, engine, fixture_pgn) -> None:
    """Re-running on the same game should upsert, not duplicate."""
    analyse_game(fixture_pgn, journal=tmp_journal, engine=engine, external_id="idem-1", depth=6)
    analyse_game(fixture_pgn, journal=tmp_journal, engine=engine, external_id="idem-1", depth=6)
    with tmp_journal.read() as conn:
        games = conn.execute("SELECT COUNT(*) AS n FROM games").fetchone()["n"]
        positions = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    assert games == 1
    assert positions == 33


def test_analyse_spawns_a_drill_per_instructive_mistake(
    tmp_journal, engine, fixture_pgn
) -> None:
    """Imported games must feed the drill queue, not just live play.

    Before this, `insert_drill` was only ever called from `api_play.py`,
    so a user who imported a year of Lichess games still opened an empty
    Drills tab — the SRS path had nothing to schedule.
    """
    summary = analyse_game(
        fixture_pgn, journal=tmp_journal, engine=engine,
        external_id="drills-1", depth=6,
    )
    with tmp_journal.read() as conn:
        drills = conn.execute("SELECT COUNT(*) AS n FROM drills").fetchone()["n"]
        instructive = conn.execute(
            "SELECT COUNT(*) AS n FROM mistakes WHERE instructive = 1"
        ).fetchone()["n"]
    assert instructive == summary.instructive_mistakes
    assert drills == instructive


def test_reanalysis_does_not_duplicate_mistakes_or_drills(
    tmp_journal, engine, fixture_pgn
) -> None:
    """`mistakes.position_id` has no uniqueness constraint, so a second
    pass used to append a whole second set of rows — double-counting
    every profile statistic and doubling the review queue."""
    analyse_game(
        fixture_pgn, journal=tmp_journal, engine=engine,
        external_id="dup-1", depth=6,
    )
    with tmp_journal.read() as conn:
        first_mistakes = conn.execute("SELECT COUNT(*) AS n FROM mistakes").fetchone()["n"]
        first_drills = conn.execute("SELECT COUNT(*) AS n FROM drills").fetchone()["n"]

    analyse_game(
        fixture_pgn, journal=tmp_journal, engine=engine,
        external_id="dup-1", depth=6,
    )
    with tmp_journal.read() as conn:
        second_mistakes = conn.execute("SELECT COUNT(*) AS n FROM mistakes").fetchone()["n"]
        second_drills = conn.execute("SELECT COUNT(*) AS n FROM drills").fetchone()["n"]
        per_position = conn.execute(
            "SELECT COUNT(*) AS n FROM mistakes GROUP BY position_id ORDER BY n DESC LIMIT 1"
        ).fetchone()

    assert second_mistakes == first_mistakes
    assert second_drills == first_drills
    # The real invariant: at most one mistake row per position, ever.
    assert per_position is None or per_position["n"] == 1


def test_reanalysis_retires_drills_that_stop_being_instructive(
    tmp_journal, engine, fixture_pgn, monkeypatch
) -> None:
    """A deeper second pass can downgrade a mistake out of "instructive".

    The drill spawned by the shallower pass must not outlive that
    judgement — nothing else in the system ever revisits it, so it would
    sit in the queue forever. It is retired rather than deleted, because
    `drills.mistake_id` is ON DELETE CASCADE and a delete would take the
    user's attempt history with it.

    The downgrade is forced rather than hoped for: whether a real depth
    change flips any particular move is engine-version-dependent, which
    would make this test vacuous on some machines and flaky on others.
    """
    analyse_game(
        fixture_pgn, journal=tmp_journal, engine=engine,
        external_id="retire-1", depth=6,
    )
    with tmp_journal.read() as conn:
        live = conn.execute(
            "SELECT COUNT(*) AS n FROM drills WHERE retired = 0"
        ).fetchone()["n"]
    assert live > 0, "fixture must spawn at least one drill to retire"

    real_classify = analyze_mod.classify_move

    def never_instructive(**kwargs):
        verdict = real_classify(**kwargs)
        if verdict is None:
            return None
        return dataclasses.replace(verdict, instructive=False)

    monkeypatch.setattr(analyze_mod, "classify_move", never_instructive)
    analyse_game(
        fixture_pgn, journal=tmp_journal, engine=engine,
        external_id="retire-1", depth=6,
    )

    with tmp_journal.read() as conn:
        still_live = conn.execute(
            "SELECT COUNT(*) AS n FROM drills WHERE retired = 0"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM drills").fetchone()["n"]
    assert still_live == 0
    # Retired, not deleted — the rows and their attempt history survive.
    assert total >= live


def test_analyse_records_user_moves_only(tmp_journal, engine, fixture_pgn) -> None:
    """Mistakes are recorded for the user's (white's) moves only, not the opponent's."""
    summary = analyse_game(
        fixture_pgn,
        journal=tmp_journal,
        engine=engine,
        external_id="morphy-user-only",
        depth=6,
    )
    # Morphy plays white. Game has 33 half-moves → 17 white moves.
    # At shallow depth the engine may not flag many as instructive; we
    # only assert the *total* mistake rows are within a sane bound.
    with tmp_journal.read() as conn:
        mistakes = conn.execute("SELECT COUNT(*) AS n FROM mistakes").fetchone()["n"]
    # White made 17 moves; mistakes rows must be <= 17.
    assert mistakes <= 17
    # And the journal must reflect the split between instructive and
    # rejected (the negative class, per the Coin Scout lesson).
    assert summary.instructive_mistakes + summary.rejected_candidates == mistakes


def test_analyse_records_threshold_in_force(tmp_journal, engine, fixture_pgn) -> None:
    """Every mistake row must carry the threshold that was active when it was written."""
    analyse_game(
        fixture_pgn, journal=tmp_journal, engine=engine,
        external_id="morphy-thresh", depth=6,
    )
    with tmp_journal.read() as conn:
        # Find any rejected row (instructive=0) and verify the threshold
        # is non-null and reflects the user's rating (1500 → 115).
        rows = conn.execute(
            "SELECT threshold_cp_in_force FROM mistakes WHERE instructive = 0 LIMIT 1"
        ).fetchall()
        if rows:
            assert rows[0]["threshold_cp_in_force"] in (115, 100)  # 115 @ 1500, 100 fallback


def test_analyse_handles_trivial_pgn(tmp_journal, engine) -> None:
    """A 1-move game (1.f3 e5 1-0) doesn't crash."""
    pgn = '[Event "T"] [Result "*"]\n\n1. f3 e5 *\n'
    summary = analyse_game(
        pgn, journal=tmp_journal, engine=engine, external_id="trivial", depth=4,
    )
    assert summary.total_positions == 2
    with tmp_journal.read() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    assert n == 2


def test_analyse_rejects_unparseable_pgn(tmp_journal, engine) -> None:
    # python-chess is lenient: a string with no PGN headers still
    # parses to a zero-move game. We reject that as "not a real game."
    with pytest.raises(ValueError):
        analyse_game("not a pgn", journal=tmp_journal, engine=engine, external_id="bad")
    # But a syntactically valid PGN with no moves also doesn't make sense.
    # The error path is "no mainline moves" rather than "no game at all."
    with pytest.raises(ValueError):
        analyse_game(
            '[Event "Empty"]\n\n*',
            journal=tmp_journal, engine=engine, external_id="empty",
        )
