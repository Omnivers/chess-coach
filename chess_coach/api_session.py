"""Phase 5 — the daily session spine (drills -> game -> review) and the
long-run profile that #/study renders.

Every average/rate here follows the null-not-zero rule: an empty
denominator returns `None`, never `0` or `0.0` — see `review.summarise`,
which already does this for accuracy/ACPL and is reused rather than
re-derived. Row counts (games played, drills due) are genuine counts and
stay `0` when empty.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .journal import (
    Journal,
    get_or_create_session,
    session_streak,
    update_session,
    utc_now_iso,
)
from .review import phase_for_ply, summarise, user_move_evals

# How many drills a day's session targets before "Drills" counts as done.
# A single named constant so the number lives in one place, not scattered
# across the schedule-init logic and any future UI copy.
DAILY_DRILL_TARGET = 8

# Clock thresholds (ms) for the two blunder-rate buckets in `Profile`.
_FAST_CLOCK_MS = 30_000
_SLOW_CLOCK_MS = 120_000

# A game counts as played once it has a result. `games.result` is '*'
# while a game is still on the board, and those rows outlive the game:
# a tab closed after three moves leaves a permanent 3-ply "game" behind.
# Counting them would inflate the denominator of every per-game rate
# below, and hand `_acpl_stats` fragments whose `total_plies` is so small
# that `phase_for_ply` calls the third move an endgame.
_COMPLETED_GAME = "result != '*'"


class SessionStep(BaseModel):
    key: str
    label: str
    done: bool


class SessionToday(BaseModel):
    day: str
    streak: int
    drills_done: int
    drills_total: int
    reviewed: bool
    game_external_id: Optional[str]
    steps: list[SessionStep]


class SessionUpdate(BaseModel):
    game_external_id: Optional[str] = None
    reviewed: Optional[bool] = None
    drills_done: Optional[int] = None


class MotifCount(BaseModel):
    motif: str
    count: int


class Profile(BaseModel):
    games_played: int
    rating_estimate: Optional[float]
    acpl_overall: Optional[float]
    acpl_by_phase: dict[str, Optional[float]]
    hints_per_game: Optional[float]
    guard_fire_rate: Optional[float]
    blunder_rate_under_30s: Optional[float]
    blunder_rate_over_120s: Optional[float]
    drills_due: int
    weakest_motifs: list[MotifCount]


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_or_init_session(conn: sqlite3.Connection, *, day: str) -> sqlite3.Row:
    """Fetch today's session row, seeding `drills_total` on first touch.

    A brand-new day's row has `drills_total = 0`, which would render as
    "0/0 drills" — indistinguishable from "nothing due." Setting it once,
    the first time the row is read, to how many drills are actually due
    (capped at the daily target) is what makes the fraction mean
    something. Must run inside a write transaction since it can persist.
    """
    row = get_or_create_session(conn, day=day)
    if row["drills_total"] == 0:
        due_now = conn.execute(
            "SELECT COUNT(*) AS n FROM drills WHERE retired = 0 AND due_at <= ?",
            (utc_now_iso(),),
        ).fetchone()["n"]
        update_session(conn, day=day, drills_total=min(DAILY_DRILL_TARGET, due_now))
        row = get_or_create_session(conn, day=day)  # re-read what was actually stored
    return row


def _session_to_response(conn: sqlite3.Connection, row: sqlite3.Row) -> SessionToday:
    day = row["day"]
    game_external_id: Optional[str] = None
    if row["game_id"] is not None:
        g = conn.execute(
            "SELECT external_id FROM games WHERE id = ?", (row["game_id"],)
        ).fetchone()
        game_external_id = g["external_id"] if g is not None else None

    drills_done, drills_total = row["drills_done"], row["drills_total"]
    steps = [
        SessionStep(
            key="drills", label="Drills",
            done=drills_total > 0 and drills_done >= drills_total,
        ),
        SessionStep(key="game", label="Game", done=row["game_id"] is not None),
        SessionStep(key="review", label="Review", done=bool(row["reviewed"])),
    ]
    return SessionToday(
        day=day, streak=session_streak(conn, today=day),
        drills_done=drills_done, drills_total=drills_total,
        reviewed=bool(row["reviewed"]), game_external_id=game_external_id,
        steps=steps,
    )


def _games_played(conn: sqlite3.Connection) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM games WHERE {_COMPLETED_GAME}"
    ).fetchone()["n"]


def _rating_estimate(conn: sqlite3.Connection) -> Optional[float]:
    """Mean `my_rating` over the 10 most recent rated games.

    "Most recent" and "has a rating" are combined in one ORDER BY/LIMIT:
    the 10 most recent games that carry a rating, not the 10 most recent
    games with unrated ones silently coerced to nothing.
    """
    rows = conn.execute(
        """
        SELECT my_rating FROM games
         WHERE my_rating IS NOT NULL
         ORDER BY played_at DESC LIMIT 10
        """
    ).fetchall()
    if not rows:
        return None
    return sum(r["my_rating"] for r in rows) / len(rows)


def _acpl_stats(conn: sqlite3.Connection) -> tuple[Optional[float], dict[str, Optional[float]]]:
    """Overall + per-phase ACPL across every game, via `review.summarise`.

    `total_plies` per game is fetched once as a grouped query rather than
    per-game — an N+1 otherwise, since every game would re-query it.
    """
    totals = {
        r["game_id"]: r["max_ply"]
        for r in conn.execute("SELECT game_id, MAX(ply) AS max_ply FROM positions GROUP BY game_id")
    }
    games = conn.execute(
        f"SELECT id, color FROM games WHERE {_COMPLETED_GAME}"
    ).fetchall()

    all_pairs: list[tuple[int, int]] = []
    phase_pairs: dict[str, list[tuple[int, int]]] = {
        "opening": [], "middlegame": [], "endgame": [],
    }
    for g in games:
        total_plies = totals.get(g["id"])
        if not total_plies:
            continue
        for move in user_move_evals(conn, game_id=g["id"], user_color=g["color"]):
            pair = (move.before_cp, move.after_cp)
            all_pairs.append(pair)
            phase_pairs[phase_for_ply(move.ply, total_plies)].append(pair)

    _, acpl_overall = summarise(all_pairs)
    acpl_by_phase = {phase: summarise(pairs)[1] for phase, pairs in phase_pairs.items()}
    return acpl_overall, acpl_by_phase


def _hints_per_game(conn: sqlite3.Connection, games_played: int) -> Optional[float]:
    if games_played == 0:
        return None
    n = conn.execute("SELECT COUNT(*) AS n FROM hint_events").fetchone()["n"]
    return n / games_played


def _guard_fire_rate(conn: sqlite3.Connection, games_played: int) -> Optional[float]:
    """Fraction of games in which the blunder guard fired at least once —
    distinct games, not raw event count, since a game can trip it repeatedly.
    """
    if games_played == 0:
        return None
    n = conn.execute("SELECT COUNT(DISTINCT game_id) AS n FROM guard_events").fetchone()["n"]
    return n / games_played


def _clock_blunder_rate(conn: sqlite3.Connection, *, faster_than_ms: Optional[int], slower_than_ms: Optional[int]) -> Optional[float]:
    """Fraction of the user's own clocked moves that were blunders, in one
    clock-time bucket.

    Ply parity encodes the journal's convention that ply 1 is white to
    move (see `review.side_to_move_at`): odd plies are white's, even
    plies are black's, and a move is "the user's" when that parity
    matches `games.color`.
    """
    clock_clause = "p.clock_ms < ?" if faster_than_ms is not None else "p.clock_ms > ?"
    threshold = faster_than_ms if faster_than_ms is not None else slower_than_ms
    where = f"""
        p.clock_ms IS NOT NULL AND {clock_clause} AND p.move_san IS NOT NULL
        AND g.{_COMPLETED_GAME}
        AND ((p.ply % 2 = 1 AND g.color = 'white') OR (p.ply % 2 = 0 AND g.color = 'black'))
    """
    denom = conn.execute(
        f"SELECT COUNT(*) AS n FROM positions p JOIN games g ON g.id = p.game_id WHERE {where}",
        (threshold,),
    ).fetchone()["n"]
    if denom == 0:
        return None
    numer = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM positions p
        JOIN games g ON g.id = p.game_id
        JOIN mistakes m ON m.position_id = p.id AND m.severity = 'blunder'
        WHERE {where}
        """,
        (threshold,),
    ).fetchone()["n"]
    return numer / denom


def _weakest_motifs(conn: sqlite3.Connection) -> list[MotifCount]:
    """Top-5 motifs across instructive mistakes, most common first.

    Ties break on motif name so the ordering is deterministic — a
    dict-iteration-order tie would otherwise make this test-flaky.
    """
    rows = conn.execute(
        "SELECT motifs FROM mistakes WHERE instructive = 1 AND motifs IS NOT NULL AND motifs != ''"
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        for m in (row["motifs"] or "").split(","):
            m = m.strip()
            if m:
                counts[m] = counts.get(m, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [MotifCount(motif=m, count=n) for m, n in ranked[:5]]


def build_router(journal: Journal) -> APIRouter:
    r = APIRouter()

    @r.get("/session/today", response_model=SessionToday)
    def session_today_get() -> SessionToday:
        with journal.transaction() as conn:
            row = _load_or_init_session(conn, day=_today())
            return _session_to_response(conn, row)

    @r.post("/session/today", response_model=SessionToday)
    def session_today_post(body: SessionUpdate) -> SessionToday:
        day = _today()
        with journal.transaction() as conn:
            _load_or_init_session(conn, day=day)

            if body.game_external_id is not None:
                g = conn.execute(
                    "SELECT id FROM games WHERE external_id = ?", (body.game_external_id,)
                ).fetchone()
                if g is None:
                    raise HTTPException(status_code=404, detail="game not found")
                update_session(conn, day=day, game_id=g["id"])

            if body.reviewed is not None:
                update_session(conn, day=day, reviewed=1 if body.reviewed else 0)

            if body.drills_done is not None:
                if body.drills_done < 0:
                    raise HTTPException(status_code=400, detail="drills_done must be >= 0")
                update_session(conn, day=day, drills_done=body.drills_done)

            row = get_or_create_session(conn, day=day)
            return _session_to_response(conn, row)

    @r.get("/stats/profile", response_model=Profile)
    def stats_profile() -> Profile:
        with journal.read() as conn:
            games_played = _games_played(conn)
            acpl_overall, acpl_by_phase = _acpl_stats(conn)
            drills_due = conn.execute(
                "SELECT COUNT(*) AS n FROM drills WHERE retired = 0 AND due_at <= ?",
                (utc_now_iso(),),
            ).fetchone()["n"]
            return Profile(
                games_played=games_played,
                rating_estimate=_rating_estimate(conn),
                acpl_overall=acpl_overall,
                acpl_by_phase=acpl_by_phase,
                hints_per_game=_hints_per_game(conn, games_played),
                guard_fire_rate=_guard_fire_rate(conn, games_played),
                blunder_rate_under_30s=_clock_blunder_rate(
                    conn, faster_than_ms=_FAST_CLOCK_MS, slower_than_ms=None
                ),
                blunder_rate_over_120s=_clock_blunder_rate(
                    conn, faster_than_ms=None, slower_than_ms=_SLOW_CLOCK_MS
                ),
                drills_due=drills_due,
                weakest_motifs=_weakest_motifs(conn),
            )

    return r


__all__ = [
    "build_router",
    "DAILY_DRILL_TARGET",
    "SessionStep",
    "SessionToday",
    "SessionUpdate",
    "MotifCount",
    "Profile",
]
