"""Phase 1 — Stockfish UCI subprocess wrapper.

These tests verify the engine wrapper around an actual Stockfish binary.
Skip them if stockfish isn't on PATH (`pytest -m "not engine"`), so the
suite still runs on machines without the engine installed.

The contract is: UCI round-trip is correct, multipv returns the right
number of lines, cp and mate are parsed correctly, and the process is
cleanly shut down.
"""
from __future__ import annotations

import shutil

import pytest

from chess_coach.engine import (
    DEFAULT_MULTIPV,
    EngineError,
    EngineLine,
    EngineOptions,
    StockfishEngine,
    _looks_like_uci,
    _parse_info_line,
)


pytestmark = pytest.mark.engine


@pytest.fixture(scope="module")
def engine() -> StockfishEngine:
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    return StockfishEngine(options=EngineOptions(threads=1, hash_mb=32, multipv=3))


def test_engine_initialises(engine: StockfishEngine) -> None:
    # No exception = handshake succeeded.
    assert engine is not None


def test_engine_returns_multipv_lines(engine: StockfishEngine) -> None:
    lines = engine.analyse(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        depth=10,
        multipv=3,
    )
    assert len(lines) == 3
    assert all(isinstance(line, EngineLine) for line in lines)
    ranks = [line.multipv_rank for line in lines]
    assert ranks == [1, 2, 3]


def test_engine_first_line_is_e4_on_start(engine: StockfishEngine) -> None:
    """Starting position at modest depth: e4 is the canonical first choice."""
    lines = engine.analyse(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        depth=12,
        multipv=1,
    )
    assert lines[0].best_uci == "e2e4"
    assert lines[0].cp is not None
    assert lines[0].mate is None
    assert lines[0].cp > 0  # White's first move should be slightly favoured


def test_engine_parses_mate_scores(engine: StockfishEngine) -> None:
    """Position with a clear mate-in-1: Ra1-a8# on the back rank."""
    # White rook on a1, black king on g8, all pawns removed to clear the file.
    fen = "6k1/5ppp/8/8/8/8/8/R6K w - - 0 1"
    lines = engine.analyse(fen, depth=8, multipv=1)
    assert len(lines) >= 1
    best = lines[0]
    assert best.mate == 1, f"expected mate=1, got cp={best.cp} mate={best.mate}"
    assert best.cp is None
    assert best.best_uci == "a1a8"


def test_engine_captures_have_pv(engine: StockfishEngine) -> None:
    """A non-trivial position: PV should be a non-empty list of UCI moves."""
    lines = engine.analyse(
        "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 2 3",
        depth=12,
        multipv=3,
    )
    for line in lines:
        assert line.pv
        assert all(_looks_like_uci(move) for move in line.pv)
        assert line.best_uci == line.pv[0]


def test_engine_close_is_idempotent() -> None:
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    eng = StockfishEngine()
    eng.close()
    eng.close()  # second close should not raise


def test_engine_missing_binary_raises() -> None:
    with pytest.raises(EngineError):
        StockfishEngine(binary="/nonexistent/path/stockfish-xyz")


def test_parse_info_line_basic() -> None:
    info = _parse_info_line(
        "info depth 12 seldepth 14 multipv 1 score cp 27 nodes 1234 "
        "nps 100000 hashfull 0 tbhits 0 time 12 pv e2e4 e7e5 g1f3"
    )
    assert info is not None
    assert info["depth"] == 12
    assert info["multipv"] == 1
    assert info["cp"] == 27
    assert info["pv"] == ["e2e4", "e7e5", "g1f3"]


def test_parse_info_line_mate() -> None:
    info = _parse_info_line(
        "info depth 8 multipv 1 score mate 1 nodes 100 pv a1a8"
    )
    assert info is not None
    assert info["mate"] == 1
    assert info["cp"] is None  # we never set cp when mate is parsed
    assert info["pv"] == ["a1a8"]


def test_looks_like_uci_accepts_valid_moves() -> None:
    assert _looks_like_uci("e2e4")
    assert _looks_like_uci("e7e8q")
    assert _looks_like_uci("a1h8")


def test_looks_like_uci_rejects_garbage() -> None:
    assert not _looks_like_uci("e2e9")     # rank 9 doesn't exist
    assert not _looks_like_uci("xyz")      # not 4 chars
    assert not _looks_like_uci("e2e4x")    # bad promotion
    assert not _looks_like_uci("")
    assert not _looks_like_uci("E2E4")     # UCI is lowercase


def test_default_multipv_is_sensible() -> None:
    """The default multipv should be > 1 so the UI can show alternates."""
    assert DEFAULT_MULTIPV >= 2


def test_engine_context_manager_closes_cleanly() -> None:
    if shutil.which("stockfish") is None:
        pytest.skip("stockfish binary not on PATH")
    with StockfishEngine() as eng:
        # Confirm a query works.
        lines = eng.analyse(
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            depth=8,
            multipv=1,
        )
        assert lines
    # On exit, the engine should be closed — calling close again is fine.
