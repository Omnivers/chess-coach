#!/usr/bin/env python3
"""chess_day.py — thin CLI launcher for the Chess Coach app.

Stdlib only (argparse, urllib.request, json, webbrowser, subprocess, sys, os,
time) so it can be shelled out to from Hermes (or a cron job, or a terminal)
without any project dependency being importable.

Commands:
    chess_day.py status   print today's session status as text
    chess_day.py start    ensure the server is up, open the browser at #/study
    chess_day.py serve    start the server in the background, print the URL

Base URL comes from CHESS_COACH_URL, default http://127.0.0.1:8787.

Exit codes: 0 success, 1 server unreachable / could not be started,
2 bad usage (argparse's default for a missing/invalid subcommand).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8787"
STUDY_PATH = "/#/study"
HEALTH_TIMEOUT_S = 3.0
START_TIMEOUT_S = 20.0
POLL_INTERVAL_S = 0.5


class ApiError(Exception):
    """A request to the chess-coach API failed."""


class ChessDayError(Exception):
    """A local precondition (venv, repo layout) was not met."""


def get_base_url() -> str:
    """Resolve the API base URL from the environment."""
    return os.environ.get("CHESS_COACH_URL", DEFAULT_BASE_URL).rstrip("/")


def repo_root() -> Path:
    """Resolve the repo root from this file's location, symlink-safe."""
    return Path(__file__).resolve().parent.parent


def fetch_json(base: str, path: str, timeout: float = HEALTH_TIMEOUT_S) -> dict[str, Any]:
    """GET base+path and parse the JSON body, or raise ApiError."""
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ApiError(f"GET {path} failed: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"GET {path} returned invalid JSON: {exc}") from exc


def check_health(base: str) -> dict[str, Any] | None:
    """Return the /health payload, or None if the server is unreachable."""
    try:
        return fetch_json(base, "/health", timeout=HEALTH_TIMEOUT_S)
    except ApiError:
        return None


def safe_fetch(base: str, path: str) -> dict[str, Any] | None:
    """Like fetch_json, but swallows ApiError into None for optional data."""
    try:
        return fetch_json(base, path)
    except ApiError:
        return None


def _fmt_streak(streak: Any) -> str:
    if streak is None:
        return "not enough data yet"
    return f"{streak} day(s)"


def build_status_report(base: str, session: dict[str, Any] | None, stats: dict[str, Any] | None) -> str:
    """Build the human status report. Never prints a fabricated 0 for null data."""
    lines = [f"Chess Coach — status ({base})", ""]
    if session is None:
        lines.append("Today's session: not enough data yet (session endpoint unavailable)")
    else:
        due = session.get("drills_due")
        done = session.get("drills_done")
        total = session.get("drills_total")
        due_txt = "not enough data yet" if due is None else f"{due} due"
        if due is not None and done is not None and total is not None:
            due_txt += f" ({done}/{total} done today)"
        lines.append(f"Warm-up drills: {due_txt}")
        game_id = session.get("game_id")
        game_ext = session.get("game_external_id")
        lines.append(
            f"Today's game: played ({game_ext or game_id})" if game_id else "Today's game: not played yet"
        )
        reviewed = session.get("reviewed")
        review_txt = "not enough data yet" if reviewed is None else ("done" if reviewed else "not done yet")
        lines.append(f"Review: {review_txt}")
        lines.append(f"Streak: {_fmt_streak(session.get('streak'))}")
    if stats is None:
        lines.append("Top recurring motif: not enough data yet (stats endpoint unavailable)")
    else:
        motifs = stats.get("weakest_motifs") or []
        if motifs:
            top = motifs[0]
            lines.append(f"Top recurring motif: {top.get('motif')} ({top.get('count')}x in last 20 games)")
        else:
            lines.append("Top recurring motif: not enough data yet")
    return "\n".join(lines)


def cmd_status(base: str) -> int:
    """`status`: print today's session, non-fabricated."""
    if check_health(base) is None:
        print(f"Chess Coach server is not reachable at {base}.")
        print(f"Start it with: {Path(__file__).name} serve")
        return 1
    session = safe_fetch(base, "/session/today")
    stats = safe_fetch(base, "/stats/profile")
    print(build_status_report(base, session, stats))
    return 0


def _host_port(base: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(base)
    return parsed.hostname or "127.0.0.1", str(parsed.port or 8787)


def manual_start_command(root: Path, base: str) -> str:
    host, port = _host_port(base)
    return f"cd {root} && .venv/bin/python -m uvicorn chess_coach.api:app --host {host} --port {port}"


def start_server(root: Path, base: str) -> None:
    """Launch uvicorn detached, logging to data/server.log. Raises ChessDayError on setup problems."""
    venv_python = root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        raise ChessDayError(
            f"No venv found at {venv_python}.\n"
            "Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
        )
    host, port = _host_port(base)
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "server.log"
    cmd = [str(venv_python), "-m", "uvicorn", "chess_coach.api:app", "--host", host, "--port", port]
    with open(log_path, "ab") as log_file:
        subprocess.Popen(
            cmd,
            cwd=str(root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def wait_for_health(base: str, timeout: float = START_TIMEOUT_S) -> bool:
    """Poll /health until it answers or timeout elapses."""
    deadline = time.monotonic() + timeout
    while True:
        if check_health(base) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_S)


def ensure_server_running(base: str, root: Path) -> int:
    """Shared logic for start/serve: start uvicorn if needed. Returns 0/1."""
    try:
        start_server(root, base)
    except ChessDayError as exc:
        print(f"Could not start the server: {exc}")
        return 1
    if wait_for_health(base):
        return 0
    print(f"Server did not come up within {int(START_TIMEOUT_S)}s.")
    print(f"Check the log: {root / 'data' / 'server.log'}")
    print(f"Or run it by hand: {manual_start_command(root, base)}")
    return 1


def cmd_serve(base: str, root: Path) -> int:
    """`serve`: start the server in the background, print the URL. Never a second copy."""
    if check_health(base) is not None:
        print(f"Chess Coach server is already running at {base}")
        return 0
    rc = ensure_server_running(base, root)
    if rc == 0:
        print(f"Chess Coach server is up at {base}")
    return rc


def cmd_start(base: str, root: Path) -> int:
    """`start`: ensure the server is up, then open the browser at #/study."""
    if check_health(base) is None:
        print("Server not running yet, starting it...")
        rc = ensure_server_running(base, root)
        if rc != 0:
            return rc
        print(f"Chess Coach server is up at {base}")
    webbrowser.open(f"{base}{STUDY_PATH}")
    print(f"Opened {base}{STUDY_PATH}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chess_day.py", description="Daily launcher for the Chess Coach app.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Print today's session status")
    sub.add_parser("start", help="Ensure the server is up and open the study view")
    sub.add_parser("serve", help="Start the server in the background")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = get_base_url()
    root = repo_root()
    if args.command == "status":
        return cmd_status(base)
    if args.command == "start":
        return cmd_start(base, root)
    if args.command == "serve":
        return cmd_serve(base, root)
    return 2  # unreachable: argparse enforces a valid subcommand


if __name__ == "__main__":
    sys.exit(main())
