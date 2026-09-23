"""Phase 6 — tests for the coach chat briefing builders in `api_coach.py`.

These exercise the pure functions directly (`_build_briefing`,
`_compose_messages`, `_live_game_facts`) rather than the `/coach/chat`
endpoint itself: the endpoint's own behaviour is just "stream whatever
Hermes says", which needs a running gateway to test meaningfully and adds
nothing over testing the gateway-agnostic pieces in isolation. No network
call and no real Stockfish process is needed for any test here.

Fixture shape copied from `tests/test_api_session.py`: `open_journal` on a
tmp_path DB, `insert_game` inside `journal.transaction()` for seeding.
"""
from __future__ import annotations

from pathlib import Path

import chess
import pytest

from chess_coach.api_coach import ChatMessage, _build_briefing, _compose_messages
from chess_coach.engine import EngineLine
from chess_coach.journal import insert_game, open_journal, utc_now_iso
from chess_coach.play import LiveGame


@pytest.fixture
def empty_journal(tmp_path: Path):
    return open_journal(tmp_path / "coach.db")


class _FakePool:
    """Stands in for `EnginePool` — no Stockfish process involved.

    `analyst_analyse` either returns one canned `EngineLine` or raises,
    depending on `fail`, so both the "engine answered" and "engine hiccup"
    paths through `_live_game_facts` get exercised without a real engine.
    """

    def __init__(self, best_uci: str = "e2e4", fail: bool = False):
        self.best_uci = best_uci
        self.fail = fail

    def analyst_analyse(self, fen: str, *, depth: int = 18, multipv: int = 1):
        if self.fail:
            raise RuntimeError("engine process died")
        return [
            EngineLine(
                multipv_rank=1, depth=depth, cp=35, mate=None,
                best_uci=self.best_uci, pv=[self.best_uci, "e7e5"],
            )
        ]


def _make_live_game(*, terminated: bool = False) -> LiveGame:
    board = chess.Board()
    return LiveGame(
        game_id="live-1",
        board=board,
        user_color=chess.WHITE,
        engine_color=chess.BLACK,
        db_id=1,
        started_at=0.0,
        terminated=terminated,
        result="win" if terminated else None,
    )


# --- _build_briefing on an empty journal -----------------------------------


def test_build_briefing_empty_journal_nonempty_and_no_raise(empty_journal) -> None:
    text = _build_briefing(empty_journal, None, None, "fr", "play")
    assert isinstance(text, str)
    assert text.strip() != ""


def test_build_briefing_empty_journal_tolerates_none_pool_and_route(empty_journal) -> None:
    # No live game means the pool is never touched, so `pool=None` and
    # `route=None` must both be safe — this is the exact call shape used
    # to eyeball the briefing offline (no live server, no engine).
    text = _build_briefing(empty_journal, None, None, "en", None)
    assert isinstance(text, str)
    assert text.strip() != ""


# --- language switch ---------------------------------------------------


def test_build_briefing_lang_fr_contains_french(empty_journal) -> None:
    text = _build_briefing(empty_journal, None, None, "fr", "play")
    assert "élève" in text
    assert "Règles pour ta réponse" in text


def test_build_briefing_lang_en_contains_english(empty_journal) -> None:
    text = _build_briefing(empty_journal, None, None, "en", "play")
    assert "student" in text
    assert "Rules for your reply" in text


# --- ROADMAP.md §0: no move/PV/motif leakage before the game is over ----


def test_unfinished_live_game_never_leaks_best_move(empty_journal) -> None:
    lg = _make_live_game(terminated=False)
    pool = _FakePool(best_uci="e2e4")
    text = _build_briefing(empty_journal, pool, lg, "fr", "play")

    assert "e2e4" not in text
    # The engine eval itself is allowed (it's not "a move, a piece, or a
    # square") — only the best move / PV / motifs are withheld.
    assert "35 centipawns" in text
    # The rule must be stated explicitly, not just silently obeyed.
    assert "ne nomme jamais un coup" in text or "RAPPEL" in text


def test_unfinished_live_game_degrades_gracefully_when_engine_unavailable(empty_journal) -> None:
    """The engine hiccup path: analyst_analyse raises, briefing must still build."""
    lg = _make_live_game(terminated=False)
    pool = _FakePool(fail=True)
    text = _build_briefing(empty_journal, pool, lg, "fr", "play")
    assert isinstance(text, str)
    assert text.strip() != ""
    assert "e2e4" not in text


def test_terminated_live_game_may_reveal_best_move(empty_journal) -> None:
    lg = _make_live_game(terminated=True)
    pool = _FakePool(best_uci="e2e4")
    text = _build_briefing(empty_journal, pool, lg, "fr", "play")
    assert "e2e4" in text
    assert "terminée" in text


def test_terminated_live_game_degrades_gracefully_when_engine_unavailable(empty_journal) -> None:
    lg = _make_live_game(terminated=True)
    pool = _FakePool(fail=True)
    text = _build_briefing(empty_journal, pool, lg, "fr", "play")
    assert isinstance(text, str)
    assert text.strip() != ""


# --- _compose_messages ---------------------------------------------------


def test_compose_messages_briefing_in_system_and_last_user_turn() -> None:
    briefing = "BRIEFING-MARKER-XYZ"
    messages = [
        ChatMessage(role="user", content="Bonjour"),
        ChatMessage(role="assistant", content="Salut !"),
        ChatMessage(role="user", content="Où sont mes parties ?"),
    ]
    composed = _compose_messages(briefing, messages, "fr")

    assert composed[0]["role"] == "system"
    assert composed[0]["content"] == briefing

    # Original order and content are preserved for every non-final turn.
    assert composed[1]["role"] == "user"
    assert composed[1]["content"] == "Bonjour"
    assert composed[2]["role"] == "assistant"
    assert composed[2]["content"] == "Salut !"

    # The briefing is re-injected around the LAST user message only.
    last = composed[3]
    assert last["role"] == "user"
    assert briefing in last["content"]
    assert "Où sont mes parties ?" in last["content"]
    assert last["content"] != briefing  # it's wrapped, not just replaced


def test_compose_messages_does_not_mutate_input(empty_journal) -> None:
    original = [
        ChatMessage(role="user", content="Bonjour"),
    ]
    snapshot = [m.model_copy() for m in original]
    _compose_messages("some briefing", original, "fr")
    assert original == snapshot


def test_compose_messages_no_user_message_falls_back_to_leading_turn() -> None:
    briefing = "BRIEFING-MARKER"
    messages = [ChatMessage(role="assistant", content="hi")]
    composed = _compose_messages(briefing, messages, "en")
    assert composed[0]["role"] == "system"
    assert any(m["role"] == "user" and briefing in m["content"] for m in composed)


# --- seeded journal shows up in the record -------------------------------


def test_build_briefing_seeded_games_mentions_record(empty_journal) -> None:
    with empty_journal.transaction() as conn:
        insert_game(
            conn, source="local", external_id="g1", played_at=utc_now_iso(),
            color="white", result="win", pgn="", opening_name="Italian Game", eco="C50",
        )
        insert_game(
            conn, source="local", external_id="g2", played_at=utc_now_iso(),
            color="black", result="loss", pgn="",
        )
    text = _build_briefing(empty_journal, None, None, "fr", "review")
    assert "2 parties enregistrées" in text
    assert "Italian Game" in text
