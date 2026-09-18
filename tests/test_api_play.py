"""Phase 5 — live play against Stockfish.

These tests verify the play endpoints end-to-end: starting a game,
playing a move, getting the engine's reply, persisting positions to
the journal, and detecting checkmate/stalemate termination.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient

from chess_coach.api import create_app
from chess_coach.enginepool import EnginePool
from chess_coach.journal import open_journal


@pytest.fixture(scope="module")
def pool():
    """One pool for the module — two engine processes, closed at teardown.

    `create_app` builds its own pool when none is passed; the tests supply
    one so the processes are torn down deterministically rather than left
    to the app's lifespan.
    """
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    p = EnginePool.create()
    yield p
    p.close()


@pytest.fixture
def tmp_journal(tmp_path: Path):
    return open_journal(tmp_path / "play.db")


@pytest.fixture
def client(tmp_journal, pool) -> TestClient:
    return TestClient(create_app(tmp_journal, pool=pool))


def test_play_new_starts_game(client) -> None:
    resp = client.post("/play/new", json={"user_color": "white", "engine_elo": 1200})
    assert resp.status_code == 200
    body = resp.json()
    assert "game_id" in body
    assert body["user_color"] == "white"
    assert body["engine_color"] == "black"
    assert body["is_user_turn"] is True
    # Starting FEN is the standard starting position.
    board = chess.Board(body["fen"])
    assert board.turn == chess.WHITE


def test_play_new_black_gets_engine_first_move(client) -> None:
    """If user picks black, engine (white) moves first."""
    resp = client.post("/play/new", json={"user_color": "black", "engine_elo": 1200})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_color"] == "black"
    assert body["is_user_turn"] is True  # after the engine's first move
    board = chess.Board(body["fen"])
    assert board.turn == chess.BLACK
    # Engine just played (e.g. 1.e4). The next ply to be played is 2
    # (black's first move).
    assert body["ply"] == 2


def test_play_new_rejects_bad_color(client) -> None:
    resp = client.post("/play/new", json={"user_color": "red", "engine_elo": 1500})
    assert resp.status_code == 400


def test_play_new_rejects_bad_elo(client) -> None:
    resp = client.post("/play/new", json={"user_color": "white", "engine_elo": 100})
    assert resp.status_code == 400


def test_play_move_gets_engine_reply(client, tmp_journal) -> None:
    """User plays e4; engine replies with some move; game continues."""
    new = client.post("/play/new", json={"user_color": "white", "engine_elo": 1200}).json()
    game_id = new["game_id"]
    resp = client.post(
        f"/play/{game_id}/move",
        json={"uci": "e2e4", "clock_ms": 600_000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_move_san"] == "e4"
    assert body["user_move_uci"] == "e2e4"
    assert body["engine_move_san"] is not None  # engine replied
    assert body["engine_move_uci"] is not None
    assert body["terminated"] is False
    # Two plies now: 1 (e4) + 1 (engine reply). is_user_turn should be True again.
    assert body["is_user_turn"] is True

    # Journal should have the game + 3 positions (start, after e4, after engine reply).
    with tmp_journal.read() as conn:
        games = conn.execute("SELECT COUNT(*) AS n FROM games").fetchone()["n"]
        positions = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    assert games == 1
    assert positions >= 3


def test_play_move_rejects_illegal(client) -> None:
    new = client.post("/play/new", json={"user_color": "white", "engine_elo": 1200}).json()
    game_id = new["game_id"]
    # e2e5 isn't legal from the starting position.
    resp = client.post(
        f"/play/{game_id}/move", json={"uci": "e2e5"},
    )
    assert resp.status_code == 400


def test_play_move_rejects_garbage(client) -> None:
    new = client.post("/play/new", json={"user_color": "white", "engine_elo": 1200}).json()
    game_id = new["game_id"]
    resp = client.post(
        f"/play/{game_id}/move", json={"uci": "junk"},
    )
    assert resp.status_code == 400


def test_play_state_returns_fen_and_history(client) -> None:
    new = client.post("/play/new", json={"user_color": "white", "engine_elo": 1200}).json()
    game_id = new["game_id"]
    client.post(f"/play/{game_id}/move", json={"uci": "e2e4"})
    state = client.get(f"/play/{game_id}").json()
    assert state["game_id"] == game_id
    # ply_of: "the next ply to be played in this position."
    # After e2e4 + engine reply, board is at fullmove=2, turn=WHITE →
    # next ply is 3.
    assert state["ply"] == 3
    assert "e4" in state["move_history"]
    assert state["terminated"] is False


def test_play_resign_records_loss(client, tmp_journal) -> None:
    new = client.post("/play/new", json={"user_color": "white", "engine_elo": 1200}).json()
    game_id = new["game_id"]
    resp = client.post(f"/play/{game_id}/resign")
    assert resp.status_code == 200
    assert resp.json()["result"] == "loss"  # white resigned → loss for white
    # Journal updated.
    with tmp_journal.read() as conn:
        row = conn.execute(
            "SELECT result FROM games WHERE external_id = ?", (game_id,),
        ).fetchone()
    assert row["result"] == "loss"


def test_play_resign_black_records_loss(client, tmp_journal) -> None:
    """Whoever resigns loses. Black user resigning → result='loss'."""
    new = client.post("/play/new", json={"user_color": "black", "engine_elo": 1200}).json()
    game_id = new["game_id"]
    resp = client.post(f"/play/{game_id}/resign")
    assert resp.status_code == 200
    assert resp.json()["result"] == "loss"
    with tmp_journal.read() as conn:
        row = conn.execute(
            "SELECT result FROM games WHERE external_id = ?", (game_id,),
        ).fetchone()
    assert row["result"] == "loss"


def test_play_404_for_unknown_game(client) -> None:
    resp = client.post("/play/does-not-exist/move", json={"uci": "e2e4"})
    assert resp.status_code == 404
    resp = client.get("/play/does-not-exist")
    assert resp.status_code == 404


def test_play_move_terminated_rejected(client) -> None:
    new = client.post("/play/new", json={"user_color": "white", "engine_elo": 1200}).json()
    game_id = new["game_id"]
    client.post(f"/play/{game_id}/resign")
    resp = client.post(f"/play/{game_id}/move", json={"uci": "e2e4"})
    assert resp.status_code == 400


# --- strength ladder -------------------------------------------------
#
# `engine_elo` used to travel the whole stack — request body, `games`
# row, `live_state` mirror, response — without ever reaching Stockfish:
# `grep -r UCI_Elo chess_coach/` returned nothing. Every game, at every
# setting, was played by a full-strength engine at depth 12. These tests
# pin the two halves of the fix: that beginner ratings are accepted, and
# that weakening the opponent does not contaminate the journal.


def test_play_new_accepts_beginner_elo(client) -> None:
    """The floor used to be 1200, which is not a beginner opponent."""
    resp = client.post("/play/new", json={"user_color": "white", "engine_elo": 800})
    assert resp.status_code == 200
    assert resp.json()["engine_elo"] == 800


def test_persisted_evals_stay_full_strength_at_a_weak_elo(
    client, tmp_journal,
) -> None:
    """The opponent is handicapped; the *evaluation* must not be.

    At 600 the opponent searches to depth 1 — a score from that search is
    noise, and it is the number that feeds `mistakes`, ACPL and every
    downstream statistic. So the move comes from the weakened opponent
    engine and the eval from the full-strength analyst. The observable
    difference is search depth: if the eval rows were still coming from
    the opponent, they would carry the opponent's depth cap.
    """
    from chess_coach.strength import strength_for_elo

    weak_depth = strength_for_elo(600).depth
    new = client.post(
        "/play/new", json={"user_color": "white", "engine_elo": 600},
    ).json()
    client.post(f"/play/{new['game_id']}/move", json={"uci": "e2e4"})

    with tmp_journal.read() as conn:
        depths = [
            r["depth"] for r in conn.execute("SELECT depth FROM evals").fetchall()
        ]
    assert depths, "the move should have written eval rows"
    assert all(d > weak_depth for d in depths)


# --- pause -------------------------------------------------------------
#
# The clock is client-reported (`_apply_user_clock` never measures wall
# time itself), so the only way to make a pause real rather than a UI
# trick is to have the server refuse moves/guards/hints while paused. These
# tests pin that refusal, that pausing/resuming round-trips the flag and
# clock correctly, and that a terminated game can't be paused.


def test_play_pause_and_resume_report_flag_and_clock(client) -> None:
    new = client.post(
        "/play/new", json={"user_color": "white", "engine_elo": 1200},
    ).json()
    game_id = new["game_id"]

    paused = client.post(f"/play/{game_id}/pause", json={"paused": True})
    assert paused.status_code == 200
    body = paused.json()
    assert body["paused"] is True
    assert body["clock"] is not None

    resumed = client.post(f"/play/{game_id}/pause", json={"paused": False})
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False


def test_play_move_rejected_while_paused_and_not_applied(client) -> None:
    new = client.post(
        "/play/new", json={"user_color": "white", "engine_elo": 1200},
    ).json()
    game_id = new["game_id"]
    client.post(f"/play/{game_id}/pause", json={"paused": True})

    before_fen = client.get(f"/play/{game_id}").json()["fen"]
    resp = client.post(f"/play/{game_id}/move", json={"uci": "e2e4"})
    assert resp.status_code == 409

    after_fen = client.get(f"/play/{game_id}").json()["fen"]
    assert after_fen == before_fen


def test_play_hint_rejected_while_paused_and_no_credit_spent(client) -> None:
    new = client.post(
        "/play/new", json={"user_color": "white", "engine_elo": 1200},
    ).json()
    game_id = new["game_id"]
    credits_before = client.get(f"/play/{game_id}").json()["hint_credits"]
    client.post(f"/play/{game_id}/pause", json={"paused": True})

    resp = client.post(f"/play/{game_id}/hint", json={"tier": 1})
    assert resp.status_code == 409

    credits_after = client.get(f"/play/{game_id}").json()["hint_credits"]
    assert credits_after == credits_before


def test_play_guard_rejected_while_paused(client) -> None:
    new = client.post(
        "/play/new", json={"user_color": "white", "engine_elo": 1200},
    ).json()
    game_id = new["game_id"]
    client.post(f"/play/{game_id}/pause", json={"paused": True})

    resp = client.post(f"/play/{game_id}/guard", json={"uci": "e2e4"})
    assert resp.status_code == 409


def test_play_move_succeeds_after_resume(client) -> None:
    new = client.post(
        "/play/new", json={"user_color": "white", "engine_elo": 1200},
    ).json()
    game_id = new["game_id"]
    client.post(f"/play/{game_id}/pause", json={"paused": True})
    client.post(f"/play/{game_id}/pause", json={"paused": False})

    resp = client.post(f"/play/{game_id}/move", json={"uci": "e2e4"})
    assert resp.status_code == 200
    assert resp.json()["user_move_san"] == "e4"


def test_play_state_reports_paused_flag(client) -> None:
    new = client.post(
        "/play/new", json={"user_color": "white", "engine_elo": 1200},
    ).json()
    game_id = new["game_id"]
    assert client.get(f"/play/{game_id}").json()["paused"] is False

    client.post(f"/play/{game_id}/pause", json={"paused": True})
    assert client.get(f"/play/{game_id}").json()["paused"] is True


def test_play_pause_rejected_for_terminated_game(client) -> None:
    new = client.post(
        "/play/new", json={"user_color": "white", "engine_elo": 1200},
    ).json()
    game_id = new["game_id"]
    client.post(f"/play/{game_id}/resign")

    resp = client.post(f"/play/{game_id}/pause", json={"paused": True})
    assert resp.status_code == 400
