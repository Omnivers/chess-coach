"""Phase 6 — tests for the /review endpoints.

Fixture shape copied from `tests/test_api.py` so this stays consistent
with the rest of the API test suite: a module-scoped Stockfish engine
(skipped when the binary isn't on PATH), the Morphy Opera Game as
deterministic seed data, and a plain FastAPI TestClient.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import chess_coach.api_coach as api_coach
from chess_coach.analyze import analyse_game
from chess_coach.api import create_app
from chess_coach.engine import EngineOptions, StockfishEngine
from chess_coach.journal import open_journal

FIXTURE = Path("data/fixtures/morphy_opera.pgn")


@pytest.fixture(scope="module")
def engine() -> StockfishEngine:
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    return StockfishEngine(options=EngineOptions(threads=1, hash_mb=32, multipv=2))


@pytest.fixture
def seeded_journal(tmp_path: Path, engine):
    """Build a journal with one analysed game."""
    pgn_text = FIXTURE.read_text()
    j = open_journal(tmp_path / "api.db")
    analyse_game(
        pgn_text, journal=j, engine=engine,
        external_id="morphy-api", depth=6,
    )
    return j


@pytest.fixture
def client(seeded_journal) -> TestClient:
    return TestClient(create_app(seeded_journal))


def test_review_detail_shape(client) -> None:
    resp = client.get("/review/morphy-api")
    assert resp.status_code == 200
    body = resp.json()

    moves = body["moves"]
    assert moves  # non-empty
    for i, move in enumerate(moves):
        assert move["ply"] == i

    for mistake in body["mistakes"]:
        assert 0 <= mistake["ply"] < len(moves)
    for highlight in body["highlights"]:
        assert 0 <= highlight["ply"] < len(moves)

    accuracy = body["accuracy"]
    assert accuracy is None or (isinstance(accuracy, (int, float)) and 0 <= accuracy <= 100)
    acpl = body["acpl"]
    assert acpl is None or (isinstance(acpl, int) and acpl >= 0)


def test_review_404_for_unknown_game(client) -> None:
    resp = client.get("/review/does-not-exist")
    assert resp.status_code == 404


def test_review_pgn_download(client) -> None:
    resp = client.get("/review/morphy-api/pgn")
    assert resp.status_code == 200
    assert resp.text.startswith("[Event ")


def test_review_summary_forces_local_source(client, monkeypatch) -> None:
    """Monkeypatch the availability probe so this test never touches the
    network — it must always take the local-template fallback path."""

    async def fake_check_availability() -> tuple[bool, str]:
        return False, "test"

    monkeypatch.setattr(api_coach, "_check_availability", fake_check_availability)

    resp = client.get("/review/morphy-api/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"]
    assert body["source"] == "local"


def test_at_least_one_move_has_an_eval(client) -> None:
    """Every move but the last should carry a next-position eval; assert
    on "at least one" rather than pinning down the exact last index, so
    this doesn't depend on how many plies the fixture happens to have."""
    resp = client.get("/review/morphy-api")
    moves = resp.json()["moves"]
    assert any(m["eval_cp"] is not None for m in moves)
