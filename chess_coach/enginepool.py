"""Phase 5+ — a pool of two engines, one per job.

ROADMAP §1.3 ("One engine, two jobs") identifies the bug this module
fixes: a single `StockfishEngine` was being reused for both the live
opponent move (fast, single line, must not block) and the analyst work
(deeper, multipv, used for review/highlights/hints/guard). Because
`StockfishEngine` is explicitly NOT thread-safe (see engine.py's
docstring — the UCI protocol is request/response, not safe for
concurrent callers), sharing one process invites interleaved
`position`/`go` calls corrupting each other's output the moment two
requests overlap (e.g. a guard check firing while the opponent is
still thinking).

`EnginePool` gives each job its own process and its own lock:

- `opponent`: multipv=1, threads=1, hash_mb=64 — the in-game reply engine.
  It only ever returns *moves* (`opponent_move`); it is strength-limited
  per game, so its scores are not evidence and are never persisted.
- `analyst`: multipv=3, threads=2, hash_mb=128 — review, guard, hints,
  highlight detection.

Each engine is guarded by its own `threading.Lock`, held for the full
duration of a call so two requests against the same engine queue up
rather than interleave. The two engines never share a lock, so an
opponent move and an analyst call can run concurrently.

Engine start is lazy-tolerant: if Stockfish isn't on PATH or fails to
spawn, the pool records the failure and keeps serving `None`/raises a
clear `EngineError` per call rather than crashing the app at import or
construction time. `/health` reports `engine: false` in that case, and
review/drills/stats — none of which need a live process — keep working.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .engine import DEFAULT_DEPTH, EngineError, EngineLine, EngineOptions, StockfishEngine
from .strength import strength_for_elo


# In-game engine calls use a shallower depth than review/analyst calls so a
# reply doesn't stall the client for a human-perceptible amount of time.
LIVE_DEPTH = 12

OPPONENT_OPTIONS = EngineOptions(multipv=1, threads=1, hash_mb=64)
ANALYST_OPTIONS = EngineOptions(multipv=3, threads=2, hash_mb=128)


def _try_start(options: EngineOptions) -> tuple[Optional[StockfishEngine], Optional[str]]:
    """Start an engine, returning (engine, error_message).

    Exactly one of the two is None. Never raises — a failed start is
    data, not an exception, so callers (including app startup) can't
    accidentally crash on a missing binary.
    """
    try:
        return StockfishEngine(options=options), None
    except EngineError as exc:
        return None, str(exc)
    except Exception as exc:  # pragma: no cover - defensive, unexpected spawn failure
        return None, f"unexpected error starting engine: {exc}"


@dataclass
class EnginePool:
    """Two independently-locked Stockfish processes: opponent and analyst."""

    opponent: Optional[StockfishEngine]
    analyst: Optional[StockfishEngine]
    opponent_error: Optional[str] = None
    analyst_error: Optional[str] = None
    _opponent_lock: threading.Lock = field(default_factory=threading.Lock)
    _analyst_lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def create(cls) -> "EnginePool":
        """Start both engines, tolerating individual failures."""
        opponent, opp_err = _try_start(OPPONENT_OPTIONS)
        analyst, an_err = _try_start(ANALYST_OPTIONS)
        return cls(
            opponent=opponent,
            analyst=analyst,
            opponent_error=opp_err,
            analyst_error=an_err,
        )

    @property
    def available(self) -> bool:
        """True iff at least the opponent engine is usable (drives /health)."""
        return self.opponent is not None and self.analyst is not None

    def opponent_move(self, fen: str, *, elo: int) -> Optional[str]:
        """The opponent's reply at `elo`, as a UCI move — or None if there is none.

        Deliberately returns a *move* and not an `EngineLine`. A weakened
        engine's evaluation is not evidence of anything: Skill Level
        randomises move choice and the depth cap here can be as low as 1,
        so its score would be noise. Persisting it would feed that noise
        into `mistakes` and ACPL, which is precisely the "engine output is
        the truth layer" guarantee the project rests on. Callers that need
        an eval for the same position must ask `analyst_analyse`.

        The strength options are re-applied on every call. They are sticky
        on the process and the pool is shared across concurrent games, so
        the engine's current setting is never safe to assume.
        """
        if self.opponent is None:
            raise EngineError(self.opponent_error or "opponent engine not available")
        strength = strength_for_elo(elo)
        with self._opponent_lock:
            self.opponent.apply_strength(strength)
            lines = self.opponent.analyse(fen, depth=strength.depth, multipv=1)
        return lines[0].best_uci if lines else None

    def analyst_analyse(
        self, fen: str, *, depth: int = DEFAULT_DEPTH, multipv: int = 3
    ) -> list[EngineLine]:
        """Analyse `fen` with the analyst engine (multipv up to 3), lock held for the call."""
        if self.analyst is None:
            raise EngineError(self.analyst_error or "analyst engine not available")
        with self._analyst_lock:
            return self.analyst.analyse(fen, depth=depth, multipv=multipv)

    def close(self) -> None:
        """Close both engines. Tolerates one (or both) already being dead/absent."""
        for eng in (self.opponent, self.analyst):
            if eng is None:
                continue
            try:
                eng.close()
            except Exception:
                pass


__all__ = [
    "EnginePool",
    "LIVE_DEPTH",
    "OPPONENT_OPTIONS",
    "ANALYST_OPTIONS",
]
