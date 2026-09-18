# Setup

Full setup for running Chess Coach locally. See `README.md` for the short
version and `docs/API.md` for the HTTP contract.

## 1. Python

Requires Python 3.10+ (developed against 3.14). The system `python3` on
macOS is often much older (3.9) — use a dedicated venv:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.txt` lists `chess`, `pytest`, `httpx`, `fastapi`, `pydantic`.
`uvicorn` is the ASGI server that runs the app; if your install doesn't pull
it in, add it directly:

```bash
.venv/bin/python -m pip install uvicorn
```

## 2. Stockfish

The truth layer. Nothing in the app evaluates a position without it.

**macOS:**

```bash
brew install stockfish
```

**Linux (Debian/Ubuntu):**

```bash
sudo apt-get install stockfish
```

**Linux (other distros):** build or download a binary from
[stockfishchess.org/download](https://stockfishchess.org/download/) and make
sure `stockfish` is on `PATH`.

Verify with `GET /health` once the server is running — its `engine` field is
`false` if Stockfish can't be reached.

## 3. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CHESS_COACH_DB` | `data/journal.db` | Path to the SQLite journal the server opens on startup. |
| `CHESS_COACH_WEB` | `web/index.html` | Path to the HTML shell the server reads fresh on every `GET /`. |
| `CHESS_COACH_URL` | `http://127.0.0.1:8787` | Base URL `scripts/chess_day.py` talks to. Only relevant if you run the server on a different host or port. |
| `LICHESS_TOKEN` | none | Optional personal Lichess API token, raises the ingest rate limit. Read from process env first, then `~/.hermes/.env`. Unauthenticated ingest works, just slower. |
| `HERMES_API_BASE` | `http://127.0.0.1:8642/v1` | Upstream Hermes OpenAI-compatible endpoint the optional coach chat panel talks to. |
| `HERMES_API_KEY` / `API_SERVER_KEY` | none | Bearer credential for `HERMES_API_BASE`. See §6 below — do not put the value in this repo. |

None of these need to be set for the core app (play, review, ingest without
a token) to work.

## 4. Running tests

```bash
.venv/bin/pytest -v
```

## 5. Ingesting Lichess games

The project's own Lichess username for ingest and testing is **`Omnivers`**
(a public handle, not a secret). To pull and analyze the back-catalogue:

```bash
.venv/bin/python -c "
from chess_coach.engine import StockfishEngine, EngineOptions
from chess_coach.journal import open_journal
from chess_coach.ingest import ingest_user
with StockfishEngine(options=EngineOptions(multipv=3)) as eng, \
     open_journal('data/journal.db') as j:
    stats = ingest_user('Omnivers', journal=j, engine=eng, max_games=20)
    print(stats)
"
```

Drop `max_games` for the full history, or set `LICHESS_TOKEN` first to raise
the rate limit for a large pull. Ingest is idempotent and resumable — running
it again only fetches games newer than the last one already in the journal.

To analyze a single local PGN file instead of pulling from Lichess, use
`chess_coach.analyze.analyse_game` — see `README.md`'s CLI usage snippet.

## 6. Optional: the Hermes gateway (coach chat)

The coach chat panel in the web UI (when built) talks to a **Hermes**
OpenAI-compatible endpoint at `http://127.0.0.1:8642/v1`, authenticated with
a bearer token from `API_SERVER_KEY` (or `HERMES_API_KEY`).

**As of this writing, that gateway platform is not enabled by default and is
not running on this machine.** Until it is:

- The coach chat panel shows as offline (`GET /coach/status` reports
  `available: false` with a reason).
- Every other feature — play, review, ingest, drills — works normally.
  Nothing in this app is gated on the chat panel being reachable.

To check whether the gateway is up:

```bash
curl -s http://127.0.0.1:8642/v1/models
```

No response, a connection error, or a non-2xx status means it's not running.
Enabling it means turning on the `api_server` platform in your own Hermes
gateway configuration — that configuration lives outside this repo, in your
Hermes install, and is out of scope here.

**Credentials:** never put a token or key value in this repository. Set
`API_SERVER_KEY` (and `LICHESS_TOKEN`, if you use one) as a shell environment
variable or in `~/.hermes/.env`, which this project's loader reads but which
is never committed.

## 7. Optional: the `/chess` Hermes skill

If you also run Hermes as a personal agent and want to type `/chess` to
start the day's session, see `skill/install.sh`. It
symlinks `skill/SKILL.md` and `scripts/` into
`~/.hermes/skills/games/chess/` so that editing this repo updates the live
skill. It is not run automatically by anything in this repo — review it and
run it yourself:

```bash
./skill/install.sh
```

This step is entirely optional. The app has no dependency on Hermes being
installed.
