"""Phase 3 — FastAPI data layer.

These tests verify the HTTP contract: status codes, JSON shapes,
CORS headers, and that the journal queries return the right data.
Uses FastAPI's TestClient (in-process) so we don't need a running
server. The seed data comes from the Morphy Opera Game fixture so
the tests are deterministic and don't touch the network.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import chess
import chess.pgn
import io
import pytest
from fastapi.testclient import TestClient

from chess_coach.analyze import analyse_game
from chess_coach.api import create_app
from chess_coach.engine import EngineOptions, StockfishEngine
from chess_coach.journal import SCHEMA_VERSION, open_journal


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


def test_health(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # Whatever the journal was initialized to — asserted against the
    # constant so a schema bump doesn't silently strand this test.
    assert body["schema_version"] == SCHEMA_VERSION


def test_list_games_returns_seeded_game(client) -> None:
    resp = client.get("/games")
    assert resp.status_code == 200
    games = resp.json()
    assert len(games) == 1
    g = games[0]
    assert g["external_id"] == "morphy-api"
    assert g["source"] == "lichess"
    assert g["color"] == "white"
    assert "instructive_mistakes" in g
    assert "rejected_candidates" in g


def test_list_games_supports_limit_and_offset(client) -> None:
    resp = client.get("/games?limit=10&offset=0")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_game_returns_positions_and_evals(client) -> None:
    resp = client.get("/games/morphy-api")
    assert resp.status_code == 200
    body = resp.json()
    assert body["external_id"] == "morphy-api"
    assert body["total_plies"] == 33
    assert len(body["positions"]) == 33
    # First position has at least one eval (multipv line).
    first = body["positions"][0]
    assert first["ply"] == 1
    assert first["fen"]
    assert first["evals"]
    assert first["evals"][0]["best_uci"]
    # PV is a list of UCI moves.
    assert isinstance(first["evals"][0]["pv"], list)


def test_get_game_404_for_unknown(client) -> None:
    resp = client.get("/games/does-not-exist")
    assert resp.status_code == 404


def test_cors_header_for_vite_origin(client) -> None:
    """Vite dev server on :5173 needs CORS to call us."""
    resp = client.get(
        "/health",
        headers={"Origin": "http://localhost:5173"},
    )
    assert resp.status_code == 200
    allow_origin = resp.headers.get("access-control-allow-origin", "")
    assert "localhost:5173" in allow_origin


def test_list_mistakes_returns_rows(client) -> None:
    resp = client.get("/mistakes")
    assert resp.status_code == 200
    mistakes = resp.json()
    assert isinstance(mistakes, list)
    if mistakes:
        m = mistakes[0]
        assert {"id", "game_id", "external_id", "ply", "delta_cp",
                "severity", "classification", "phase", "motifs",
                "engine_best_uci", "played_uci", "instructive",
                "threshold_cp_in_force", "rejection_reason"} <= set(m.keys())
        assert isinstance(m["motifs"], list)


def test_list_mistakes_instructive_only(client) -> None:
    resp = client.get("/mistakes?instructive_only=true")
    assert resp.status_code == 200
    rows = resp.json()
    for r in rows:
        assert r["instructive"] is True


def test_list_mistakes_empty_for_empty_journal(tmp_path) -> None:
    """No seeded games → empty mistakes list, no crash."""
    j = open_journal(tmp_path / "empty.db")
    client = TestClient(create_app(j))
    resp = client.get("/mistakes")
    assert resp.status_code == 200
    assert resp.json() == []
