# Chess Coach

A local-first chess coach for rebuilding lost skill, not for playing better
today.

**The design thesis:** Stockfish is the only authority on the position. The
LLM layer (Hermes) never evaluates, never suggests a move, and never names an
opening from its own knowledge — it only narrates what the engine and a
deterministic motif detector already found. Every explanation in this app is
traceable back to an engine line or a journal row. If the engine didn't say
it, the coach doesn't say it either.

Why that restriction, instead of just wiring up a chatty LLM tutor: an
engine-with-a-chat-window is the standard shape of "AI chess tutor" products,
and it fails quietly — LLMs are fluent and confident at unaided board
reasoning, and also frequently wrong. A coach that hands you a move you were
capable of finding yourself performs the rep for you; you feel helped and
learn nothing. See `PROJECT.md` §3 and `ROADMAP.md` §0 for the full argument.

## Status

This is a working localhost app, still early. The journal, the Lichess
ingest, Stockfish analysis, motif detection, and a play-and-review loop in
the browser exist today. A large second phase — the in-game coach rail,
spaced-repetition drills, session tracking, openings, and a design pass — is
specified in `ROADMAP.md` and not all of it is built yet. See that file for
what's real versus planned; this README does not repeat the phase table.

## Quickstart

```bash
git clone https://github.com/Omnivers/chess-coach.git
cd chess-coach
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
brew install stockfish        # macOS; see docs/SETUP.md for Linux
./scripts/serve.sh
```

Then open `http://127.0.0.1:8787`.

If your `requirements.txt` install doesn't pull in `uvicorn` yet, install it
directly: `.venv/bin/python -m pip install uvicorn`.

## Architecture

Three layers, strictly separated — see `PROJECT.md` §2 for the full rule:

```
┌────────────────────────────────────────────┐
│  COACH   Hermes (optional) — narrates only  │
│          never evaluates a position         │
├────────────────────────────────────────────┤
│  DATA    SQLite journal + FastAPI (:8787)   │
│          deterministic, no LLM, sub-ms      │
├────────────────────────────────────────────┤
│  TRUTH   Stockfish (UCI) + python-chess     │
│          the only source of "what's best"   │
└────────────────────────────────────────────┘
```

The frontend reads the Data layer for everything it draws and, only where a
chat panel is present, the Coach layer for what it says in prose.

## Features

Built:

- SQLite journal schema for games, positions, engine evals, mistakes,
  drills, and daily sessions
- Lichess game ingest (NDJSON streaming, incremental via `since`)
- Stockfish multipv analysis of imported and played games, through an
  engine pool so the opponent and the analyst never share a UCI pipe
- Deterministic motif detection (hung pieces, forks, pins, skewers,
  back-rank, discovered attacks) via `python-chess`, with an instructive
  filter so only learnable mistakes reach the journal — the same
  detectors now run on live games as on imported ones (ROADMAP §1.1)
- The in-game coach rail: a graduated hint ladder on a 6-credit budget
  and a pre-move blunder guard, neither of which names a move for free
  (ROADMAP §2)
- Post-game review with accuracy, ACPL, a "what you did right" highlight
  class alongside the mistakes, a move scrubber, and a grounded narrative
  summary that falls back to local prose when Hermes is unreachable
  (ROADMAP §3)
- A real clock with server-authoritative time control, and time-pressure
  statistics in the profile (ROADMAP §5)
- Spaced-repetition drills (SM-2) built from your own mistakes, with a
  daily session path and a streak counter (ROADMAP §5.4, §7)
- Play, drill, review, and talk to the coach in one browser app served
  from the same FastAPI process — the chat panel is embedded, so there is
  no switching to a terminal mid-game (`web/`)

Planned (see `ROADMAP.md` for the full spec and phase order):

- Openings taught backwards from your own games, not a repertoire to
  memorize (ROADMAP §4)
- A generated lesson curriculum on top of the drill scheduler
  (ROADMAP §5)
- Maia as the opponent engine instead of weakened Stockfish, so mistakes
  you learn to punish actually occur in human games (ROADMAP §8.1)

The frontend deliberately departs from ROADMAP §6: it is plain ES modules
and hand-written CSS served at `/static` rather than a Vite/React/TS
build, so the whole platform stays "one command, one port" with no build
step between an edit and a reload.

## Documentation

- `PROJECT.md` — architecture and the guardrails that don't change
- `ROADMAP.md` — the plan: phases, schema, and the reasoning behind each one
- `docs/API.md` — the HTTP contract every module builds against
- `docs/SETUP.md` — full setup, environment variables, tests, and ingest

## Data ingest

The Lichess username this project ingests from is **`Omnivers`** — a public
Lichess handle, not a secret. See `docs/SETUP.md` for the ingest command.

Analysis verdicts (severity, motifs, the instructive flag) are stored, not
recomputed on read — so games analysed by an older build keep that build's
answers. After changing anything in `classify.py`, replay the journal:

```bash
.venv/bin/python scripts/reanalyze.py            # every stored game, depth 18
.venv/bin/python scripts/reanalyze.py --depth 12 --limit 5   # quick pass
```

It is safe to re-run: each mistake is refreshed in place rather than
appended, and drills are keyed per mistake, so your spaced-repetition
schedule survives a replay.

## Optional: Hermes integration

This repo can optionally be driven from Hermes, a personal agent runtime,
via a thin `/chess` launcher skill under `skill/`.
**Nothing in the app depends on this.** Every feature above works from a
browser alone; the Hermes skill is a convenience that shells out to
`scripts/chess_day.py` to start the day's session and read back a plain-text
status report. See `docs/SETUP.md` for how to install it and what it
requires (a separate, privately-run Hermes install — not included here).

## Security

This repo ships no secrets. Where a credential is needed (`HERMES_API_KEY`,
`API_SERVER_KEY`, `LICHESS_TOKEN`), only the environment variable name is
documented — the value belongs in your shell environment or `~/.hermes/.env`,
never in this repository.

## License

MIT. See `LICENSE`.
