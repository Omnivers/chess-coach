#!/usr/bin/env python3
"""Re-run analysis over every game already in the journal.

Needed whenever the analysis pipeline changes shape — the journal stores
the *verdict* (severity, motifs, instructive flag), not enough to
recompute one, so games analysed by an older build keep that build's
answers forever. The symptom this was written for: 55 mistakes carrying
empty `motifs` and `instructive = 0`, written before `classify.py`
existed, which left the Drills tab and `weakest_motifs` permanently
empty.

Safe to re-run. `analyse_game` upserts the game and its positions, and
refreshes each mistake in place rather than appending a second row, so
drills keep the SRS schedule the user has built up on them.

    .venv/bin/python scripts/reanalyze.py [--depth 18] [--db data/journal.db]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chess  # noqa: E402

from chess_coach.analyze import analyse_game  # noqa: E402
from chess_coach.engine import DEFAULT_DEPTH, EngineOptions, StockfishEngine  # noqa: E402
from chess_coach.journal import open_journal  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reanalyze.py", description=__doc__)
    ap.add_argument("--db", default="data/journal.db", type=Path)
    ap.add_argument("--depth", default=DEFAULT_DEPTH, type=int)
    ap.add_argument("--limit", default=0, type=int, help="0 = every game")
    args = ap.parse_args(argv)

    journal = open_journal(args.db)
    with journal.read() as conn:
        rows = conn.execute(
            "SELECT external_id, pgn, color, source FROM games "
            "WHERE pgn IS NOT NULL AND pgn != '' ORDER BY played_at"
        ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("no games with a stored PGN — nothing to re-analyse")
        return 0

    engine = StockfishEngine(options=EngineOptions(threads=2, hash_mb=128, multipv=3))
    started = time.time()
    failures = 0
    try:
        for i, row in enumerate(rows, start=1):
            label = row["external_id"] or f"game-{i}"
            try:
                summary = analyse_game(
                    row["pgn"],
                    journal=journal,
                    engine=engine,
                    external_id=row["external_id"],
                    source=row["source"] or "lichess",
                    user_color=chess.WHITE if row["color"] == "white" else chess.BLACK,
                    depth=args.depth,
                )
            except Exception as exc:  # one bad PGN must not abort the batch
                failures += 1
                print(f"[{i}/{len(rows)}] {label}: FAILED — {exc}")
                continue
            print(
                f"[{i}/{len(rows)}] {label}: {summary.total_positions} positions, "
                f"{summary.instructive_mistakes} instructive"
            )
    finally:
        engine.close()

    with journal.read() as conn:
        drills = conn.execute("SELECT COUNT(*) AS n FROM drills").fetchone()["n"]
        instructive = conn.execute(
            "SELECT COUNT(*) AS n FROM mistakes WHERE instructive = 1"
        ).fetchone()["n"]
    print(
        f"\ndone in {time.time() - started:.0f}s — "
        f"{instructive} instructive mistakes, {drills} drills, {failures} failures"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
