"""Map a target Elo onto the UCI settings that actually produce it.

Until this module existed, `engine_elo` was stored on the game row, sent
to the frontend, and never once handed to Stockfish — so every "1200"
game was played by a full-strength engine at depth 12. The selector was
decoration.

Two mechanisms, because Stockfish only offers one above 1320:

- **At or above `STOCKFISH_MIN_ELO`** — `UCI_LimitStrength` + `UCI_Elo`.
  This is Stockfish's own calibration and the honest option: it is what
  the number on the button claims to mean.
- **Below it** — `UCI_Elo` simply refuses to go there (SF 19 reports
  `min 1320`), so weakness has to come from `Skill Level` plus a hard
  depth cap. A beginner needs an opponent that hangs pieces to a
  two-move tactic, and only a tiny search does that.

The sub-1320 rungs are an empirical ladder, not a calibration. Skill
Level's effect is stochastic and depth caps interact with it in ways
nobody has measured at this end of the scale, so the anchors below are
"plays roughly like someone at this rating", chosen to be *beatable*.
Treat the numbers as a UI label, not a measurement — and if the games
feel wrong, move the anchors rather than inventing a formula.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Stockfish 19: `option name UCI_Elo type spin default 1320 min 1320 max 3190`.
# Below the floor UCI_Elo is ignored, which is exactly the trap this
# module exists to avoid walking into silently.
STOCKFISH_MIN_ELO = 1320
STOCKFISH_MAX_ELO = 3190

# The weakest rung we offer. Lower than this, Skill Level 0 at depth 1
# stops getting meaningfully worse and the label would start lying.
MIN_ELO = 600

# Search depth for a full-strength in-game reply. Mirrors
# `enginepool.LIVE_DEPTH`; kept as its own constant so the strength
# ladder can be reasoned about without importing the pool.
FULL_DEPTH = 12


@dataclass(frozen=True)
class Strength:
    """The UCI settings for one rung of the ladder.

    `uci_elo` and `skill_level` are mutually exclusive: exactly one is
    set, matching which of the two mechanisms this rung uses.
    """

    limit_strength: bool
    uci_elo: Optional[int]
    skill_level: Optional[int]
    depth: int


# (target Elo, Skill Level, depth) — see the module docstring on why this
# is a table and not a curve. A request lands on the highest anchor at or
# below it, so the rungs the UI offers should match these numbers.
_WEAK_LADDER: tuple[tuple[int, int, int], ...] = (
    (600, 0, 1),
    (800, 0, 2),
    (1000, 1, 3),
    (1200, 3, 4),
)


def clamp_elo(elo: int) -> int:
    """Pull `elo` into the range the engine can actually serve."""
    return max(MIN_ELO, min(STOCKFISH_MAX_ELO, elo))


def strength_for_elo(elo: int) -> Strength:
    """The UCI settings for a target rating.

    Input is clamped rather than rejected — validation belongs at the
    HTTP boundary, and a strength function that can raise turns every
    call site into an error path for no benefit.
    """
    target = clamp_elo(elo)
    if target >= STOCKFISH_MIN_ELO:
        return Strength(
            limit_strength=True, uci_elo=target, skill_level=None, depth=FULL_DEPTH,
        )
    # Highest anchor at or below the target. The table is sorted, and
    # `clamp_elo` guarantees at least the first anchor matches.
    skill, depth = next(
        (s, d) for anchor, s, d in reversed(_WEAK_LADDER) if anchor <= target
    )
    return Strength(
        limit_strength=False, uci_elo=None, skill_level=skill, depth=depth,
    )


#: The rungs the UI should offer, weakest first. Everything at or above
#: `STOCKFISH_MIN_ELO` is Stockfish's own calibration; below it, the
#: `_WEAK_LADDER` anchors — offering a value between two anchors would
#: silently round down and make two buttons play identically.
LADDER: tuple[int, ...] = tuple(a for a, _, _ in _WEAK_LADDER) + (
    1400, 1600, 1800, 2000, 2400, 2800,
)


__all__ = [
    "LADDER",
    "MIN_ELO",
    "STOCKFISH_MAX_ELO",
    "STOCKFISH_MIN_ELO",
    "Strength",
    "clamp_elo",
    "strength_for_elo",
]
