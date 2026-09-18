"""Phase 5 — the daily session spine and the long-run profile.

Same fixture shape as `test_api.py`. The profile's null-not-zero
contract is the focus here: an empty journal must report `None` for
every average/rate, never a fabricated `0`.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chess_coach.analyze import analyse_game
from chess_coach.api import create_app
from chess_coach.engine import EngineOptions, StockfishEngine
from chess_coach.journal import insert_game, open_journal, utc_now_iso

FIXTURE = Path("data/fixtures/morphy_opera.pgn")


@pytest.fixture(scope="module")
def engine() -> StockfishEngine:
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    return StockfishEngine(options=EngineOptions(threads=1, hash_mb=32, multipv=2))


@pytest.fixture
def seeded_journal(tmp_path: Path, engine):
    pgn_text = FIXTURE.read_text()
    j = open_journal(tmp_path / "session-api.db")
    analyse_game(pgn_text, journal=j, engine=engine, external_id="morphy-session", depth=6)
    return j


@pytest.fixture
def client(seeded_journal) -> TestClient:
    return TestClient(create_app(seeded_journal))


def test_session_today_get_shape(client) -> None:
    resp = client.get("/session/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["day"] == datetime.now(timezone.utc).date().isoformat()
    assert len(body["steps"]) == 3
    assert {s["key"] for s in body["steps"]} == {"drills", "game", "review"}
    assert body["reviewed"] is False


def test_session_today_post_reviewed_persists(client) -> None:
    resp = client.post("/session/today", json={"reviewed": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reviewed"] is True
    review_step = next(s for s in body["steps"] if s["key"] == "review")
    assert review_step["done"] is True

    # A fresh GET must still see it — this is persisted, not per-request state.
    resp2 = client.get("/session/today")
    assert resp2.status_code == 200
    assert resp2.json()["reviewed"] is True


def test_session_today_post_unknown_game_404(client) -> None:
    resp = client.post("/session/today", json={"game_external_id": "does-not-exist"})
    assert resp.status_code == 404


def test_session_today_post_known_game_sets_step_done(client) -> None:
    resp = client.post("/session/today", json={"game_external_id": "morphy-session"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["game_external_id"] == "morphy-session"
    game_step = next(s for s in body["steps"] if s["key"] == "game")
    assert game_step["done"] is True


def test_profile_empty_journal_is_all_null_not_zero(tmp_path) -> None:
    """The null-not-zero contract: with no games at all, every average or
    rate must come back None. Only genuine row counts (games_played,
    drills_due) are allowed to be a real 0.
    """
    j = open_journal(tmp_path / "empty.db")
    client = TestClient(create_app(j))
    resp = client.get("/stats/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["games_played"] == 0
    assert body["drills_due"] == 0
    assert body["rating_estimate"] is None
    assert body["acpl_overall"] is None
    assert body["hints_per_game"] is None
    assert body["guard_fire_rate"] is None
    assert body["blunder_rate_under_30s"] is None
    assert body["blunder_rate_over_120s"] is None
    assert body["weakest_motifs"] == []
    # Every phase key present, even though nothing was ever analysed.
    assert set(body["acpl_by_phase"].keys()) == {"opening", "middlegame", "endgame"}
    assert all(v is None for v in body["acpl_by_phase"].values())


def test_profile_ignores_in_progress_games(seeded_journal) -> None:
    """A game still on the board is not a game played.

    `games.result` stays '*' until a game finishes, and abandoned rows
    outlive the session that made them — a browser tab closed after three
    moves leaves a 3-ply game behind forever. Counting those inflates the
    denominator of every per-game rate in the profile.
    """
    with seeded_journal.transaction() as conn:
        for i in range(4):
            insert_game(
                conn, source="local", external_id=f"abandoned-{i}",
                played_at=utc_now_iso(), color="white", result="*", pgn="",
            )
    client = TestClient(create_app(seeded_journal))
    body = client.get("/stats/profile").json()
    assert body["games_played"] == 1, "only the finished fixture game counts"


def test_profile_seeded_journal_reports_a_game(client) -> None:
    resp = client.get("/stats/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["games_played"] == 1
    assert body["acpl_overall"] is None or body["acpl_overall"] >= 0
    assert set(body["acpl_by_phase"].keys()) == {"opening", "middlegame", "endgame"}
