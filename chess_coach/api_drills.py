"""Phase 5 — drill queue endpoints (API.md §5). Read/write over `drills`.

Three endpoints back the #/drills tab: aggregate stats for the sidebar,
the due queue itself, and the attempt-submission that drives SRS
scheduling forward. Scheduling math lives in `srs.py`; this module's job
is HTTP shape + the one write transaction that has to stay atomic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import srs
from .journal import (
    Journal,
    get_or_create_session,
    insert_drill_attempt,
    list_due_drills,
    update_drill_schedule,
    update_session,
    utc_now_iso,
)

# Retention window for `DrillStats.retention_7d` — matches API.md §5's
# "7-day retention" sidebar stat.
_RETENTION_WINDOW_DAYS = 7


class DrillStats(BaseModel):
    due_now: int
    due_today: int
    total: int
    retired: int
    retention_7d: Optional[float]  # fraction correct over the last 7d, or None if no attempts


class DrillItem(BaseModel):
    id: int
    fen: str
    side_to_move: str
    motif: Optional[str]
    severity: Optional[str]
    external_id: Optional[str]
    ply: Optional[int]
    due_at: str
    reps: int
    interval_days: float


class AttemptRequest(BaseModel):
    moved_uci: str
    time_ms: Optional[int] = None


class AttemptResult(BaseModel):
    correct: bool
    solution_uci: str
    retired: bool
    interval_days: float
    reps: int
    lapses: int
    due_at: str


def _side_to_move(fen: str) -> str:
    """Second FEN field is "w"/"b". A malformed row falls back to white
    rather than raising — one bad drill must not take the whole queue down.
    """
    try:
        return "white" if fen.split()[1] == "w" else "black"
    except IndexError:
        return "white"


def build_router(journal: Journal) -> APIRouter:
    r = APIRouter()

    @r.get("/drills/stats", response_model=DrillStats)
    def drills_stats() -> DrillStats:
        now_iso = utc_now_iso()
        # UTC, not local: every other timestamp in the journal is UTC, so
        # a local date here would shift the "today" boundary by the host's
        # offset and count tomorrow's drills as due.
        today_end = datetime.now(timezone.utc).date().isoformat() + "T23:59:59+00:00"
        window_start = (
            datetime.now(timezone.utc) - timedelta(days=_RETENTION_WINDOW_DAYS)
        ).replace(microsecond=0).isoformat()
        with journal.read() as conn:
            # due_at is a sortable ISO-8601 UTC string, so plain string
            # comparison against another ISO-8601 string is correct here.
            due_now = conn.execute(
                "SELECT COUNT(*) AS n FROM drills WHERE retired = 0 AND due_at <= ?",
                (now_iso,),
            ).fetchone()["n"]
            due_today = conn.execute(
                "SELECT COUNT(*) AS n FROM drills WHERE retired = 0 AND due_at <= ?",
                (today_end,),
            ).fetchone()["n"]
            total = conn.execute("SELECT COUNT(*) AS n FROM drills").fetchone()["n"]
            retired = conn.execute(
                "SELECT COUNT(*) AS n FROM drills WHERE retired = 1"
            ).fetchone()["n"]
            attempts = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0) AS correct
                  FROM drill_attempts
                 WHERE attempted_at >= ?
                """,
                (window_start,),
            ).fetchone()
        retention = (
            attempts["correct"] / attempts["n"] if attempts["n"] > 0 else None
        )
        return DrillStats(
            due_now=due_now, due_today=due_today, total=total, retired=retired,
            retention_7d=retention,
        )

    @r.get("/drills/due", response_model=list[DrillItem])
    def drills_due(limit: int = Query(8, ge=1, le=50)) -> list[DrillItem]:
        with journal.read() as conn:
            rows = list_due_drills(conn, now_iso=utc_now_iso(), limit=limit)
        out: list[DrillItem] = []
        for row in rows:
            motifs = [m for m in (row["motifs"] or "").split(",") if m]
            out.append(DrillItem(
                id=row["id"], fen=row["fen"], side_to_move=_side_to_move(row["fen"]),
                motif=motifs[0] if motifs else None, severity=row["severity"],
                external_id=row["external_id"], ply=row["ply"], due_at=row["due_at"],
                reps=row["reps"], interval_days=row["interval_days"],
            ))
        return out

    @r.post("/drills/{drill_id}/attempt", response_model=AttemptResult)
    def submit_attempt(drill_id: int, req: AttemptRequest) -> AttemptResult:
        # One transaction: an attempt that's recorded but never rescheduled
        # (or vice versa) would desync the queue from the attempt log.
        with journal.transaction() as conn:
            row = conn.execute("SELECT * FROM drills WHERE id = ?", (drill_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="no such drill")
            if row["retired"]:
                raise HTTPException(status_code=400, detail="drill already retired")

            correct = req.moved_uci.strip().lower() == row["solution_uci"].strip().lower()
            insert_drill_attempt(
                conn, drill_id=drill_id, correct=correct,
                time_ms=req.time_ms, moved_uci=req.moved_uci,
            )

            grade = srs.grade_from_attempt(correct, req.time_ms)
            sched = srs.next_schedule(
                grade=grade, interval_days=row["interval_days"], ease=row["ease"],
                reps=row["reps"], lapses=row["lapses"], now=datetime.now(timezone.utc),
            )
            update_drill_schedule(
                conn, drill_id=drill_id, due_at=sched.due_at,
                interval_days=sched.interval_days, ease=sched.ease,
                reps=sched.reps, lapses=sched.lapses, retired=sched.retired,
            )

            # Every attempt — right or wrong — counts toward today's drill
            # quota. The daily path tracks work done, not accuracy.
            today = datetime.now(timezone.utc).date().isoformat()
            session_row = get_or_create_session(conn, day=today)
            update_session(conn, day=today, drills_done=session_row["drills_done"] + 1)

        return AttemptResult(
            correct=correct, solution_uci=row["solution_uci"], retired=sched.retired,
            interval_days=sched.interval_days, reps=sched.reps, lapses=sched.lapses,
            due_at=sched.due_at,
        )

    return r


__all__ = ["build_router", "DrillStats", "DrillItem", "AttemptRequest", "AttemptResult"]
