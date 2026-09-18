"""The engine strength ladder — `chess_coach/strength.py`.

These tests exist because `engine_elo` was, for the whole life of the
project, stored on the game row, mirrored into `live_state`, echoed to the
frontend and validated at the HTTP boundary — and never once handed to
Stockfish. Every game was played by a full-strength engine at depth 12
regardless of the button. The selector was decoration.

So the assertions below are mostly about the *wiring*: that a requested
rating produces settings that would actually reach the engine, and that
the two mechanisms (UCI_Elo above Stockfish's 1320 floor, Skill Level +
a depth cap below it) are never mixed up.
"""
from __future__ import annotations

import pytest

from chess_coach.strength import (
    LADDER,
    MIN_ELO,
    STOCKFISH_MAX_ELO,
    STOCKFISH_MIN_ELO,
    clamp_elo,
    strength_for_elo,
)


def test_at_or_above_the_stockfish_floor_uses_uci_elo() -> None:
    """Above 1320 Stockfish calibrates itself — use it, don't hand-roll."""
    s = strength_for_elo(1800)
    assert s.limit_strength is True
    assert s.uci_elo == 1800
    assert s.skill_level is None


def test_below_the_stockfish_floor_uses_skill_level_instead() -> None:
    """`UCI_Elo` reports `min 1320`; below it the option is simply ignored.

    This is the whole reason the module has two mechanisms. A rung that
    set `UCI_Elo 800` would look correct, be accepted by the engine, and
    play at 1320 — the silent failure the ladder exists to avoid.
    """
    s = strength_for_elo(800)
    assert s.limit_strength is False
    assert s.uci_elo is None
    assert s.skill_level is not None


@pytest.mark.parametrize("elo", list(LADDER))
def test_every_advertised_rung_sets_exactly_one_mechanism(elo: int) -> None:
    """`uci_elo` and `skill_level` are mutually exclusive by contract."""
    s = strength_for_elo(elo)
    assert (s.uci_elo is None) != (s.skill_level is None)


def test_weaker_ratings_never_search_deeper() -> None:
    """Depth is the main lever below 1320; it must move monotonically.

    Skill Level alone does not make an engine hang a piece to a two-move
    tactic — a tiny search does. If depth ever rose as the rating fell,
    the low rungs would quietly play stronger than the high ones.
    """
    depths = [strength_for_elo(e).depth for e in LADDER]
    assert depths == sorted(depths)


def test_out_of_range_requests_are_clamped_not_rejected() -> None:
    """Validation belongs at the HTTP boundary, not in the ladder."""
    assert clamp_elo(1) == MIN_ELO
    assert clamp_elo(99_999) == STOCKFISH_MAX_ELO
    assert strength_for_elo(1).skill_level is not None
    assert strength_for_elo(99_999).uci_elo == STOCKFISH_MAX_ELO


def test_a_request_between_rungs_rounds_down() -> None:
    """Landing on the nearest anchor *below* keeps the label honest.

    Rounding up would mean asking for 900 and getting the 1000 engine —
    an opponent stronger than the one you chose, which is precisely the
    complaint that started this.
    """
    assert strength_for_elo(900) == strength_for_elo(800)


def test_the_ui_ladder_is_sorted_and_within_range() -> None:
    """`LADDER` is what the frontend renders; it must be servable."""
    assert list(LADDER) == sorted(LADDER)
    assert LADDER[0] == MIN_ELO
    assert all(MIN_ELO <= e <= STOCKFISH_MAX_ELO for e in LADDER)


def test_the_ladder_starts_below_the_stockfish_floor() -> None:
    """The user-facing point of the whole module.

    A beginner asking for a beginner opponent must get one; before this,
    the lowest option was 1200 and it did not even do that.
    """
    assert MIN_ELO < STOCKFISH_MIN_ELO
    assert any(e < STOCKFISH_MIN_ELO for e in LADDER)
