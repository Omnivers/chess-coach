"""Phase 5 — drill queue endpoints.

Mirrors `test_api.py`'s fixture shape: a module-scoped Stockfish engine,
a journal seeded from the Morphy Opera Game fixture, and a TestClient
wrapping `create_app`. Drills aren't created by analysis itself, so each
test that needs one inserts it directly via `journal.insert_drill`.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chess_coach.analyze import analyse_game
from chess_coach.api import create_app
from chess_coach.engine import EngineOptions, StockfishEngine
from chess_coach.journal import Journal, insert_drill, open_journal

FIXTURE = Path("data/fixtures/morphy_opera.pgn")


@dataclass(frozen=True)
class SeededDrills:
    """`Journal` is itself a frozen dataclass, so the drill id/solution
    seeded for these tests are carried alongside it rather than bolted
    onto the journal instance."""

    journal: Journal
    drill_id: int
    solution_uci: str


@pytest.fixture(scope="module")
def engine() -> StockfishEngine:
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    return StockfishEngine(options=EngineOptions(threads=1, hash_mb=32, multipv=2))


@pytest.fixture
def seeded(tmp_path: Path, engine) -> SeededDrills:
    """One analysed game, plus one drill built from its first mistake."""
    pgn_text = FIXTURE.read_text()
    j = open_journal(tmp_path / "drills-api.db")
    analyse_game(pgn_text, journal=j, engine=engine, external_id="morphy-drills", depth=6)
    with j.transaction() as conn:
        mistake = conn.execute("SELECT * FROM mistakes ORDER BY id ASC LIMIT 1").fetchone()
        assert mistake is not None, "fixture must produce at least one mistake row"
        position = conn.execute(
            "SELECT fen FROM positions WHERE id = ?", (mistake["position_id"],)
        ).fetchone()
        drill_id = insert_drill(
            conn, mistake_id=mistake["id"], fen=position["fen"],
            solution_uci=mistake["engine_best_uci"],
        )
        solution_uci = mistake["engine_best_uci"]
    return SeededDrills(journal=j, drill_id=drill_id, solution_uci=solution_uci)


@pytest.fixture
def client(seeded: SeededDrills) -> TestClient:
    return TestClient(create_app(seeded.journal))


def test_drills_stats_empty_journal_reports_null_retention(tmp_path) -> None:
    """The null-not-zero regression test: an empty journal has no attempts
    in the retention window, so retention_7d must be None, never 0.0 — and
    `total` (a genuine row count) must be a real 0.
    """
    j = open_journal(tmp_path / "empty.db")
    client = TestClient(create_app(j))
    resp = client.get("/drills/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["due_now"] == 0
    assert body["retired"] == 0
    assert body["retention_7d"] is None


def test_drills_due_returns_seeded_drill(client, seeded: SeededDrills) -> None:
    resp = client.get("/drills/due?limit=8")
    assert resp.status_code == 200
    items = resp.json()
    # Analysing the fixture game spawns a drill per instructive mistake
    # (analyze.py), so the explicitly seeded drill is one of several — not
    # the only row. Assert on the one we control rather than the count.
    assert items
    item = next(i for i in items if i["id"] == seeded.drill_id)
    assert item["side_to_move"] in ("white", "black")
    assert item["fen"]
    assert item["external_id"] == "morphy-drills"
    assert all(i["side_to_move"] in ("white", "black") for i in items)


def test_attempt_correct_uci_advances_schedule(client, seeded: SeededDrills) -> None:
    resp = client.post(
        f"/drills/{seeded.drill_id}/attempt",
        json={"moved_uci": seeded.solution_uci, "time_ms": 4000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["correct"] is True
    assert body["solution_uci"] == seeded.solution_uci
    assert body["reps"] == 1
    assert body["interval_days"] == 1.0
    assert body["retired"] is False


def test_attempt_wrong_uci_reports_incorrect_and_discloses_solution(client, seeded: SeededDrills) -> None:
    # a1a1 is never a legal move, so it can never accidentally equal the solution.
    resp = client.post(f"/drills/{seeded.drill_id}/attempt", json={"moved_uci": "a1a1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["correct"] is False
    assert body["solution_uci"] == seeded.solution_uci


def test_attempt_unknown_drill_404(client) -> None:
    resp = client.post("/drills/999999/attempt", json={"moved_uci": "e2e4"})
    assert resp.status_code == 404


def test_attempt_credits_todays_session(client, seeded: SeededDrills) -> None:
    """Every attempt — right or wrong — counts toward the daily drill quota."""
    client.post(f"/drills/{seeded.drill_id}/attempt", json={"moved_uci": "a1a1"})
    resp = client.get("/session/today")
    assert resp.status_code == 200
    assert resp.json()["drills_done"] == 1
