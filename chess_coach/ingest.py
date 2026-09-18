"""Phase 1 — Lichess NDJSON ingest.

Streams a user's game archive from the Lichess HTTP API into the journal
as PGN. Position analysis is left to `analyze_game`; this module is
just network plumbing + idempotent persistence.

The API:

  GET https://lichess.org/api/games/user/{username}?max=N&clocks=true&evals=true&since=<ms>&opening=true

Returns NDJSON (one PGN-with-headers per line) when the request
advertises `application/x-ndjson`. Each game is a complete PGN with
seven-tag roster headers, which is exactly what `chess.pgn.read_game`
expects.

The (source, external_id) UNIQUE constraint on `games` makes the
ingest idempotent: re-running picks up where it left off. We expose
the high-water-mark via the Lichess `since` parameter (a unix-ms
timestamp) so Phase 6's cron can do incremental pulls.

Authentication: optional. Without a token, public rate limits apply
(~10 games/min sustained). With a token from
https://lichess.org/account/oauth/token (no scope needed for public
games), you get ~20 req/s. The token is read from the LICHESS_TOKEN
env var; we never log it.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional
from urllib.parse import urlencode

import chess
import chess.pgn

from .analyze import GameAnalysis, _header_int, _result_for_color, analyse_game
from .engine import StockfishEngine
from .journal import Journal, insert_game


LICHESS_API = "https://lichess.org/api/games/user/{username}"
USER_AGENT = "chess-coach/0.1 (https://github.com/omnivers/chess-coach)"


@dataclass
class IngestSummary:
    user: str
    games_seen: int = 0
    games_new: int = 0
    games_skipped_existing: int = 0
    last_seen_ms: Optional[int] = None
    analyses: list[GameAnalysis] = field(default_factory=list)


def _lichess_get(
    url: str,
    *,
    headers: dict,
    timeout_s: float = 60.0,
) -> Iterable[bytes]:
    """Streamed GET that yields chunks as they arrive.

    Lichess sends an infinite-ish stream of NDJSON for a user with
    many games; we don't want to buffer the whole archive in memory.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                yield chunk
    except urllib.error.HTTPError as e:
        # 429 = rate-limited; 404 = no such user; everything else is fatal.
        if e.code == 429:
            raise LichessRateLimited() from e
        if e.code == 404:
            raise LichessUserNotFound() from e
        raise


class LichessError(RuntimeError):
    """Base class for ingest errors."""


class LichessUserNotFound(LichessError):
    """The Lichess API returned 404. The user doesn't exist."""


class LichessRateLimited(LichessError):
    """The Lichess API returned 429. Caller should back off and retry."""


def _ndjson_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """Assemble a stream of bytes into NDJSON lines."""
    buf = b""
    for chunk in chunks:
        buf += chunk
        # NDJSON is one JSON object per line. We split on \n and keep the
        # tail (incomplete last line) for the next chunk.
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                yield text
    if buf.strip():
        yield buf.decode("utf-8", errors="replace").strip()


def fetch_lichess_games(
    username: str,
    *,
    max_games: Optional[int] = None,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    clocks: bool = True,
    evals: bool = True,
    opening: bool = True,
    token: Optional[str] = None,
    pgn_in_json: bool = True,
) -> Iterator[dict]:
    """Yield Lichess game JSON objects one at a time.

    With `Accept: application/x-ndjson`, the Lichess API returns one JSON
    object per line (NOT raw PGN — the PGN is embedded as a string
    field inside the JSON). The object includes game metadata and (if
    `pgn_in_json=True`) the full PGN in the `pgn` field.

    Each yielded dict has the keys Lichess sends: `id`, `pgn`, `clocks`,
    `analysis`, `players`, etc. We don't validate the schema here —
    `ingest_user` does that and skips malformed lines.
    """
    url = LICHESS_API.format(username=username)
    params: list[tuple[str, str]] = [
        ("clocks", "true" if clocks else "false"),
        ("evals", "true" if evals else "false"),
        ("opening", "true" if opening else "false"),
        ("pgnInJson", "true" if pgn_in_json else "false"),
    ]
    if max_games is not None:
        params.append(("max", str(max_games)))
    if since_ms is not None:
        params.append(("since", str(since_ms)))
    if until_ms is not None:
        params.append(("until", str(until_ms)))
    full_url = f"{url}?{urlencode(params)}"

    headers = {
        "Accept": "application/x-ndjson",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    chunks = _lichess_get(full_url, headers=headers)
    for line in _ndjson_lines(chunks):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _parse_pgn_from_game_obj(obj: dict) -> Optional[chess.pgn.Game]:
    """Extract and parse the `pgn` field from a Lichess JSON game object."""
    pgn_str = obj.get("pgn")
    if not pgn_str or not isinstance(pgn_str, str):
        return None
    g = chess.pgn.read_game(io.StringIO(pgn_str))
    if g is None:
        return None
    if not list(g.mainline_moves()):
        return None
    return g


def _lichess_timestamp_ms(game: chess.pgn.Game) -> Optional[int]:
    """Extract the Lichess `UTCDate` + `UTCTime` as unix-ms, or None."""
    date = game.headers.get("UTCDate", "")
    time_ = game.headers.get("UTCTime", "")
    if not date or not time_ or "?" in date or "?" in time_:
        return None
    # Format: "2026.09.17" + "20:00:00" → unix seconds → ms.
    try:
        dt = datetime.strptime(f"{date}T{time_}", "%Y.%m.%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _load_token_from_env() -> Optional[str]:
    """Read LICHESS_TOKEN, with priority: process env > ~/.hermes/.env.

    The Hermes-managed credential file is the primary store so that
    `hermes gateway setup`-style tooling can populate it alongside the
    Telegram token (Coin Scout shares this exact setup). We never log
    the value, and the value is read at call time — never cached on a
    module-level global where it could leak into tracebacks.
    """
    import os
    val = os.environ.get("LICHESS_TOKEN")
    if val:
        return val
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "LICHESS_TOKEN":
            return value.strip().strip('"').strip("'")
    return None


def ingest_user(
    username: str,
    *,
    journal: Journal,
    engine: StockfishEngine,
    max_games: Optional[int] = None,
    since_ms: Optional[int] = None,
    token: Optional[str] = None,
    analyse: bool = True,
    progress: Optional[Callable[[int, str], None]] = None,
) -> IngestSummary:
    """Stream games from Lichess into the journal, optionally analysing each.

    Idempotent: games that already exist (by Lichess ID) are skipped.
    The high-water-mark is computed as max(seen timestamps) + 1 so a
    subsequent call can pass `since_ms=` to pick up where we left off.
    """
    summary = IngestSummary(user=username, last_seen_ms=since_ms)
    last_seen_ms = since_ms

    # Resolve the auth token: explicit arg > process env > ~/.hermes/.env.
    if token is None:
        token = _load_token_from_env()

    for obj in fetch_lichess_games(
        username,
        max_games=max_games,
        since_ms=since_ms,
        token=token,
    ):
        game = _parse_pgn_from_game_obj(obj)
        if game is None:
            continue
        summary.games_seen += 1
        # Prefer the JSON's `id` field (stable) over the URL fragment.
        lichess_id = obj.get("id") or game.headers.get("Site", "").rsplit("/", 1)[-1] or None
        ts_ms = obj.get("createdAt")  # Lichess sends createdAt in milliseconds
        if isinstance(ts_ms, (int, float)):
            last_seen_ms = int(ts_ms)
        else:
            ts = _lichess_timestamp_ms(game)
            if ts is not None:
                last_seen_ms = ts

        # Check whether the game is already in the journal before we
        # burn engine time on it.
        existing_id = _existing_game_id(journal, "lichess", lichess_id)
        if existing_id is not None:
            summary.games_skipped_existing += 1
            if progress:
                progress(summary.games_seen, f"skip existing {lichess_id}")
            continue

        if progress:
            progress(summary.games_seen, f"new {lichess_id or '?'}")

        if analyse:
            try:
                analysis = analyse_game(
                    str(game),
                    journal=journal,
                    engine=engine,
                    source="lichess",
                    external_id=lichess_id,
                )
                summary.analyses.append(analysis)
            except Exception as exc:  # one bad game shouldn't kill the ingest
                if progress:
                    progress(summary.games_seen, f"error on {lichess_id}: {exc}")
                continue
        else:
            # Just persist the game header so Phase 1's analyse pass can
            # come back for it later.
            with journal.transaction() as conn:
                insert_game(
                    conn,
                    source="lichess",
                    external_id=lichess_id,
                    played_at=(game.headers.get("UTCDate", "????.??.??") + "T"
                               + (game.headers.get("UTCTime", "??:??:??") + "+00:00")),
                    color="white",  # unknown until analysed; default
                    result=_result_for_color(game.headers, chess.WHITE),
                    pgn=str(game),
                    time_control=game.headers.get("TimeControl"),
                    my_rating=_header_int(game.headers, "WhiteElo"),
                    opp_rating=_header_int(game.headers, "BlackElo"),
                    eco=game.headers.get("ECO"),
                    opening_name=game.headers.get("Opening"),
                )
        summary.games_new += 1

    summary.last_seen_ms = last_seen_ms
    return summary


def _existing_game_id(journal: Journal, source: str, external_id: Optional[str]) -> Optional[int]:
    if external_id is None:
        return None
    with journal.read() as conn:
        row = conn.execute(
            "SELECT id FROM games WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
    return row["id"] if row else None


__all__ = [
    "IngestSummary",
    "LichessError",
    "LichessRateLimited",
    "LichessUserNotFound",
    "fetch_lichess_games",
    "ingest_user",
]
