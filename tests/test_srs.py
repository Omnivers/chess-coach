"""Phase 2 — SM-2 spaced repetition scheduling (API.md §5).

`srs.py` is pure: no DB, no clock reads. Every test below injects `now`
explicitly so `due_at` values are exact and reproducible.
"""
from __future__ import annotations

from datetime import datetime, timezone

from chess_coach.srs import (
    GRADE_AGAIN,
    GRADE_EASY,
    GRADE_GOOD,
    GRADE_HARD,
    MIN_EASE,
    RETIRE_AFTER_REPS,
    RETIRE_MIN_INTERVAL_DAYS,
    Schedule,
    grade_from_attempt,
    next_schedule,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_grade_from_attempt_returns_again_for_wrong_answer_regardless_of_speed() -> None:
    # Arrange / Act
    grade = grade_from_attempt(correct=False, time_ms=1_000)

    # Assert
    assert grade == GRADE_AGAIN


def test_grade_from_attempt_returns_easy_for_fast_correct_answer() -> None:
    # Arrange / Act
    grade = grade_from_attempt(correct=True, time_ms=4_000)

    # Assert
    assert grade == GRADE_EASY


def test_grade_from_attempt_returns_good_for_medium_speed_correct_answer() -> None:
    # Arrange / Act
    grade = grade_from_attempt(correct=True, time_ms=15_000)

    # Assert
    assert grade == GRADE_GOOD


def test_grade_from_attempt_returns_hard_for_slow_correct_answer() -> None:
    # Arrange / Act
    grade = grade_from_attempt(correct=True, time_ms=30_000)

    # Assert
    assert grade == GRADE_HARD


def test_grade_from_attempt_treats_missing_time_as_hard_when_correct() -> None:
    # Arrange / Act
    grade = grade_from_attempt(correct=True, time_ms=None)

    # Assert
    assert grade == GRADE_HARD


def test_next_schedule_lapse_resets_reps_and_increments_lapses() -> None:
    # Arrange: a drill with some review history that then gets graded AGAIN.
    # Act
    result = next_schedule(
        grade=GRADE_AGAIN, interval_days=10.0, ease=2.5, reps=4, lapses=1, now=_NOW,
    )

    # Assert
    assert result.reps == 0
    assert result.lapses == 2
    assert result.interval_days < 1.0  # comes back almost immediately


def test_next_schedule_lapse_floors_ease_at_min_ease() -> None:
    # Arrange: ease already at the floor before the lapse.
    # Act
    result = next_schedule(
        grade=GRADE_AGAIN, interval_days=5.0, ease=MIN_EASE, reps=2, lapses=0, now=_NOW,
    )

    # Assert
    assert result.ease == MIN_EASE


def test_next_schedule_lapse_never_touches_lapses_on_a_pass() -> None:
    # Arrange / Act
    result = next_schedule(
        grade=GRADE_GOOD, interval_days=6.0, ease=2.5, reps=2, lapses=3, now=_NOW,
    )

    # Assert
    assert result.lapses == 3


def test_next_schedule_first_success_sets_interval_to_one_day() -> None:
    # Arrange: a brand-new drill, first-ever review, passed.
    # Act
    result = next_schedule(
        grade=GRADE_GOOD, interval_days=0.0, ease=2.5, reps=0, lapses=0, now=_NOW,
    )

    # Assert
    assert result.reps == 1
    assert result.interval_days == 1.0


def test_next_schedule_second_success_sets_interval_to_six_days() -> None:
    # Arrange: one prior successful review.
    # Act
    result = next_schedule(
        grade=GRADE_GOOD, interval_days=1.0, ease=2.5, reps=1, lapses=0, now=_NOW,
    )

    # Assert
    assert result.reps == 2
    assert result.interval_days == 6.0


def test_next_schedule_third_success_multiplies_interval_by_ease() -> None:
    # Arrange: two prior successful reviews.
    # Act
    result = next_schedule(
        grade=GRADE_GOOD, interval_days=6.0, ease=2.5, reps=2, lapses=0, now=_NOW,
    )

    # Assert
    assert result.reps == 3
    assert result.interval_days == 6.0 * 2.5


def test_next_schedule_due_at_matches_now_plus_interval_days() -> None:
    # Arrange / Act
    result = next_schedule(
        grade=GRADE_GOOD, interval_days=0.0, ease=2.5, reps=0, lapses=0, now=_NOW,
    )

    # Assert: first success -> interval 1.0 day.
    assert result.due_at == "2026-01-02T00:00:00+00:00"


def test_next_schedule_easy_grade_grows_ease_more_than_good_grade() -> None:
    # Arrange / Act
    easy = next_schedule(
        grade=GRADE_EASY, interval_days=6.0, ease=2.5, reps=2, lapses=0, now=_NOW,
    )
    good = next_schedule(
        grade=GRADE_GOOD, interval_days=6.0, ease=2.5, reps=2, lapses=0, now=_NOW,
    )

    # Assert
    assert easy.ease > good.ease


def test_next_schedule_retires_drill_once_reps_and_interval_thresholds_are_met() -> None:
    # Arrange: on the verge of retirement — one more good review clears both bars.
    reps_before = RETIRE_AFTER_REPS - 1
    interval_before = RETIRE_MIN_INTERVAL_DAYS  # * ease will clear the bar

    # Act
    result = next_schedule(
        grade=GRADE_GOOD, interval_days=interval_before, ease=2.5,
        reps=reps_before, lapses=0, now=_NOW,
    )

    # Assert
    assert result.reps == RETIRE_AFTER_REPS
    assert result.interval_days >= RETIRE_MIN_INTERVAL_DAYS
    assert result.retired is True


def test_next_schedule_does_not_retire_when_reps_met_but_interval_too_short() -> None:
    # Arrange: enough reps, but the interval hasn't grown past the floor yet.
    # Act
    result = next_schedule(
        grade=GRADE_GOOD, interval_days=1.0, ease=1.3,
        reps=RETIRE_AFTER_REPS, lapses=0, now=_NOW,
    )

    # Assert
    assert result.reps == RETIRE_AFTER_REPS + 1
    assert result.interval_days < RETIRE_MIN_INTERVAL_DAYS
    assert result.retired is False


def test_next_schedule_returns_a_schedule_dataclass_instance() -> None:
    # Arrange / Act
    result = next_schedule(
        grade=GRADE_GOOD, interval_days=1.0, ease=2.5, reps=1, lapses=0, now=_NOW,
    )

    # Assert
    assert isinstance(result, Schedule)
