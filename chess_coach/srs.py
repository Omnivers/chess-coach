"""Phase 2 — spaced repetition scheduling for drills (API.md §5).

SM-2 over the existing `drills` columns (`due_at`, `interval_days`,
`ease`, `reps`, `lapses`, `retired`). No new dependency — this is the
same algorithm Anki popularized, small enough to hand-roll.

Pure function: no DB. The caller reads the current schedule fields out
of a `drills` row, calls `next_schedule`, and writes the result back via
`journal.update_drill_schedule`. `now` is a parameter (not
`datetime.now()` internally) so tests are deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# SM-2 grades. Only four of the traditional 0-5 SM-2 grades are used —
# there is no partial-credit UI in the drill trainer, so an attempt is
# either wrong (AGAIN) or right, bucketed by how long it took.
GRADE_AGAIN, GRADE_HARD, GRADE_GOOD, GRADE_EASY = 0, 3, 4, 5

# SM-2's ease factor never goes below this — beneath it the algorithm's
# interval growth stops being meaningful (it would shrink instead).
MIN_EASE = 1.3

# A drill stops being scheduled once it's been reviewed this many times
# AND the interval has grown past RETIRE_MIN_INTERVAL_DAYS — both
# conditions together mean "reliably known," not just "reviewed a lot."
RETIRE_AFTER_REPS = 5
RETIRE_MIN_INTERVAL_DAYS = 21.0

# ~10 minutes, expressed in days to match `interval_days`'s unit. A
# lapsed drill comes back almost immediately rather than tomorrow.
_LAPSE_INTERVAL_DAYS = 0.007

# Time thresholds for grading a correct attempt (API.md §5).
_EASY_MS = 8_000
_GOOD_MS = 25_000


@dataclass(frozen=True)
class Schedule:
    """The next scheduling state for a drill, ready to write back."""

    due_at: str
    interval_days: float
    ease: float
    reps: int
    lapses: int
    retired: bool


def grade_from_attempt(correct: bool, time_ms: Optional[int]) -> int:
    """Map a drill attempt outcome to an SM-2 grade.

    Wrong answers are always GRADE_AGAIN regardless of how long they
    took — speed doesn't redeem an incorrect move. A missing/None
    `time_ms` on a correct attempt is treated as "took a while"
    (GRADE_HARD) since we have no evidence it was fast.
    """
    if not correct:
        return GRADE_AGAIN
    if time_ms is not None and time_ms < _EASY_MS:
        return GRADE_EASY
    if time_ms is not None and time_ms < _GOOD_MS:
        return GRADE_GOOD
    return GRADE_HARD


def next_schedule(
    *,
    grade: int,
    interval_days: float,
    ease: float,
    reps: int,
    lapses: int,
    now: datetime,
) -> Schedule:
    """Advance a drill's schedule by one SM-2 review, graded `grade`.

    `now` is injected rather than read from the clock so tests can
    assert exact `due_at` values.
    """
    if grade < GRADE_HARD:
        new_reps = 0
        new_lapses = lapses + 1
        new_interval = _LAPSE_INTERVAL_DAYS
        new_ease = max(MIN_EASE, ease - 0.2)
    else:
        new_reps = reps + 1
        if new_reps == 1:
            new_interval = 1.0
        elif new_reps == 2:
            new_interval = 6.0
        else:
            new_interval = interval_days * ease
        new_ease = max(
            MIN_EASE,
            ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)),
        )
        new_lapses = lapses

    retired = new_reps >= RETIRE_AFTER_REPS and new_interval >= RETIRE_MIN_INTERVAL_DAYS

    due_at_dt = now + timedelta(days=new_interval)
    due_at = _iso_for(due_at_dt)

    return Schedule(
        due_at=due_at,
        interval_days=new_interval,
        ease=new_ease,
        reps=new_reps,
        lapses=new_lapses,
        retired=retired,
    )


def _iso_for(when: datetime) -> str:
    """Format `when` the same way `journal.utc_now_iso` formats "now".

    `utc_now_iso` always stamps the real current instant, so it can't be
    reused directly for an arbitrary future `due_at` — but the format
    (UTC, second precision, ISO-8601) must match exactly so `parse_iso`
    round-trips it the same way.
    """
    return when.replace(microsecond=0).isoformat()


__all__ = [
    "GRADE_AGAIN",
    "GRADE_HARD",
    "GRADE_GOOD",
    "GRADE_EASY",
    "MIN_EASE",
    "RETIRE_AFTER_REPS",
    "RETIRE_MIN_INTERVAL_DAYS",
    "Schedule",
    "grade_from_attempt",
    "next_schedule",
]
