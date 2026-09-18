"""Phase 1 — Lichess NDJSON ingest.

These tests use canned NDJSON payloads and stub `_lichess_get` so the
test doesn't hit the real Lichess API. The contract under test:

- `fetch_lichess_games` yields one PGN string per line.
- NDJSON chunks are split correctly across chunk boundaries.
- `ingest_user` writes new games to the journal, skips existing ones
  (by Lichess ID), and tracks the high-water-mark.
- A 404 from Lichess becomes `LichessUserNotFound`.
- A 429 becomes `LichessRateLimited`.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

import chess.pgn
import pytest

from chess_coach.engine import EngineOptions, StockfishEngine
from chess_coach.ingest import (
    LichessError,
    LichessRateLimited,
    LichessUserNotFound,
    _lichess_get,
    _ndjson_lines,
    fetch_lichess_games,
    ingest_user,
)
from chess_coach.journal import open_journal
import chess
import io as _io


# A minimal Lichess NDJSON line: one full PGN with seven-tag roster,
# wrapped as the `pgn` field of a JSON object — which is what Lichess
# actually sends when Accept is application/x-ndjson.
import json as _json

_MORPHY_PGN = (
    '[Event "Opera Game"]\n'
    '[Site "https://lichess.org/abcdefgh"]\n'
    '[Date "1858.??.??"]\n'
    '[UTCDate "1858.01.01"]\n'
    '[UTCTime "20:00:00"]\n'
    '[White "Morphy"]\n'
    '[Black "Isouard"]\n'
    '[Result "1-0"]\n'
    '[WhiteElo "2600"]\n'
    '[BlackElo "2400"]\n'
    '[ECO "C41"]\n'
    '[Opening "Philidor"]\n'
    '[TimeControl "600+0"]\n'
    '\n'
    '1. e4 e5 2. Nf3 d6 3. d4 Bg4 4. dxe5 Bxf3 5. Qxf3 dxe5 6. Bc4 Nf6 '
    '7. Qb3 Qe7 8. Nc3 c6 9. Bg5 b5 10. Nxb5 cxb5 11. Bxb5+ Nbd7 12. O-O-O '
    'Rd8 13. Rxd7 Rxd7 14. Rd1 Qe6 15. Bxd7+ Nxd7 16. Qb8+ Nxb8 17. Rd8# 1-0\n'
)
MORPHY_LINE = _json.dumps({"id": "abcdefgh", "createdAt": -3534292800000, "pgn": _MORPHY_PGN})


# --- NDJSON parsing ----------------------------------------------------


def test_ndjson_lines_single_chunk() -> None:
    chunks = [b'{"a": 1}\n{"b": 2}\n']
    lines = list(_ndjson_lines(chunks))
    assert lines == ['{"a": 1}', '{"b": 2}']


def test_ndjson_lines_split_across_chunks() -> None:
    # First chunk ends mid-line; second chunk completes it.
    chunks = [b'{"a": 1', b'}\n{"b": 2}\n']
    lines = list(_ndjson_lines(chunks))
    assert lines == ['{"a": 1}', '{"b": 2}']


def test_ndjson_lines_ignores_blank_lines() -> None:
    chunks = [b'\n\n{"a": 1}\n\n']
    lines = list(_ndjson_lines(chunks))
    assert lines == ['{"a": 1}']


# --- fetch_lichess_games (with stubbed HTTP) ---------------------------


def _stub_lichess_get(payload: bytes, status: int = 200):
    """Replace _lichess_get with one that returns the canned payload."""

    def fake(url, *, headers, timeout_s=60.0):
        if status == 404:
            raise LichessUserNotFound()
        if status == 429:
            raise LichessRateLimited()
        yield payload

    return fake


@pytest.fixture
def engine() -> StockfishEngine:
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    return StockfishEngine(options=EngineOptions(threads=1, hash_mb=32, multipv=2))


def test_fetch_lichess_games_yields_dicts(monkeypatch) -> None:
    monkeypatch.setattr(
        "chess_coach.ingest._lichess_get", _stub_lichess_get(MORPHY_LINE.encode())
    )
    objs = list(fetch_lichess_games("anyone", max_games=1))
    assert len(objs) == 1
    obj = objs[0]
    assert obj["id"] == "abcdefgh"
    assert "pgn" in obj
    # The PGN should parse as a game.
    g = chess.pgn.read_game(_io.StringIO(obj["pgn"]))
    assert g is not None
    assert g.headers["White"] == "Morphy"


def test_fetch_lichess_games_user_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        "chess_coach.ingest._lichess_get", _stub_lichess_get(b"", status=404)
    )
    with pytest.raises(LichessUserNotFound):
        list(fetch_lichess_games("ghost"))


def test_fetch_lichess_games_rate_limited(monkeypatch) -> None:
    monkeypatch.setattr(
        "chess_coach.ingest._lichess_get", _stub_lichess_get(b"", status=429)
    )
    with pytest.raises(LichessRateLimited):
        list(fetch_lichess_games("anyone"))


# --- ingest_user end-to-end (with stubbed HTTP, real journal) ---------


def _ingest_with_shallow_depth(monkeypatch, journal, engine, *, analyse=True):
    """Stub HTTP and monkey-patch analyse_game to use depth=6 for speed."""
    payload = MORPHY_LINE.encode()
    monkeypatch.setattr("chess_coach.ingest._lichess_get", _stub_lichess_get(payload))
    from chess_coach import analyze as _analyze
    real_analyse = _analyze.analyse_game
    def shallow(pgn, *, journal, engine, **kw):
        kw.setdefault("depth", 6)
        return real_analyse(pgn, journal=journal, engine=engine, **kw)
    monkeypatch.setattr("chess_coach.ingest.analyse_game", shallow)
    return ingest_user(
        "morphy",
        journal=journal,
        engine=engine if analyse else None,  # type: ignore[arg-type]
        analyse=analyse,
    )


def test_ingest_user_writes_new_game(monkeypatch, tmp_path, engine) -> None:
    journal = open_journal(tmp_path / "ingest.db")
    summary = _ingest_with_shallow_depth(monkeypatch, journal, engine)
    assert summary.games_seen == 1
    assert summary.games_new == 1
    assert summary.games_skipped_existing == 0
    with journal.read() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM games").fetchone()["n"]
    assert n == 1


def test_ingest_user_skips_existing(monkeypatch, tmp_path, engine) -> None:
    """Re-running on the same Lichess user should upsert, not duplicate."""
    journal = open_journal(tmp_path / "ingest2.db")
    summary1 = _ingest_with_shallow_depth(monkeypatch, journal, engine)
    summary2 = _ingest_with_shallow_depth(monkeypatch, journal, engine)
    assert summary1.games_new == 1
    assert summary2.games_new == 0
    assert summary2.games_skipped_existing == 1
    with journal.read() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM games").fetchone()["n"]
    assert n == 1


def test_ingest_user_records_high_water_mark(monkeypatch, tmp_path, engine) -> None:
    """last_seen_ms is the max timestamp seen; callers use it for `since_ms`."""
    journal = open_journal(tmp_path / "ingest3.db")
    summary = _ingest_with_shallow_depth(monkeypatch, journal, engine)
    # Our fixture has createdAt=-3534292800000 (1858-01-01 in ms).
    assert summary.last_seen_ms == -3534292800000


def test_ingest_user_no_analyse_mode(monkeypatch, tmp_path) -> None:
    """analyse=False writes only the game header, no positions/evals/mistakes."""
    payload = MORPHY_LINE.encode()
    monkeypatch.setattr("chess_coach.ingest._lichess_get", _stub_lichess_get(payload))
    journal = open_journal(tmp_path / "ingest4.db")
    summary = ingest_user(
        "morphy", journal=journal, engine=None,  # type: ignore[arg-type]
        analyse=False,
    )
    assert summary.games_new == 1
    with journal.read() as conn:
        games = conn.execute("SELECT COUNT(*) AS n FROM games").fetchone()["n"]
        positions = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
        evals = conn.execute("SELECT COUNT(*) AS n FROM evals").fetchone()["n"]
    assert games == 1
    assert positions == 0
    assert evals == 0
