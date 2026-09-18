---
name: chess
description: Chess Coach daily launcher — runs scripts/chess_day.py to start today's training session. Thin trigger only; no chess knowledge lives here.
---

# Chess

Types `/chess` in Hermes and starts today's training session at
`~/Projects/chess-coach`. This skill is a **launcher, not a coach**. Per
`PROJECT.md` §3, the coaching truth lives in the Python app (Stockfish +
the journal), not in an agent's prompt — this file exists only to run a
script and read its output back to the user.

The deep chess-analysis skill (digests, weekly synthesis, "what should I
drill today") is the separate `chess-coach` skill in this same `games/`
folder. `/chess` only starts the loop; it does not analyze anything itself.

## What `/chess` does

1. Run `scripts/chess_day.py start` from the repo root:
   ```bash
   ~/Projects/chess-coach/.venv/bin/python ~/Projects/chess-coach/scripts/chess_day.py start
   ```
   This ensures the local server is up (starting it if needed) and opens
   the browser at the study view.
2. Run `scripts/chess_day.py status` the same way, and read its printed
   text. It is a plain-text report built entirely from what the app's API
   returned — nothing in it is inferred.
3. Summarize that status output back to the user in **one short
   paragraph**: how many warm-up drills are due, whether today's game has
   been played, whether it's been reviewed, the current streak, and the
   top recurring motif, if there is one.
4. If any of those fields printed as "not enough data yet", say exactly
   that — never invent a number or a trend to fill the gap.

`/chess review`, `/chess drills`, and `/chess stats` are reserved for the
deeper `chess-coach` skill's variants of this loop; this skill only
implements the bare `/chess` launch path.

## Hard rule — do not evaluate chess

**Never evaluate a position, suggest a move, or name an opening from your
own knowledge, in this skill or in reaction to what it prints.** LLMs are
fluent and confidently wrong at unaided board reasoning — that is exactly
the failure mode this project is designed around (see `ROADMAP.md` §0 and
`PROJECT.md` §3: "Hermes never evaluates a position"). You relay what
`chess_day.py` reports, nothing more.

If the user asks a chess question ("what should I have played there?",
"is my opening any good?", "what's the best move here?") while running
this skill, do not answer it yourself. Point them at the coach panel in
the web UI (the study view this skill just opened) — it is grounded in
live Stockfish output and the journal, and it is the only place in this
project allowed to discuss the position.

## The daily session this starts

From `ROADMAP.md` §7.2, "game of the day":

1. **Health check** — is the API up, is Stockfish available. `start`
   handles this by launching the server if `/health` doesn't answer.
2. **Warm-up** — drills due today from spaced repetition (capped at 8,
   roughly 4 minutes).
3. **The game** — one game, 15+10, against the Maia opponent, coach rail
   on.
4. **Review** — immediate, highlights first, then mistakes. Never
   mistakes-only; the user plays far more good moves than bad ones in a
   given game and the review should say so.
5. **Harvest** — new drills get queued from anything instructive, and
   `skill_snapshots` are written so the trend is visible later.

A curriculum "lesson" step (`ROADMAP.md` §5.3) is planned as a later
addition to this loop but is not wired in yet — don't tell the user a
lesson happened if `chess_day.py status` didn't say so.

## When the server isn't running

`chess_day.py start` starts it automatically. If it still fails after
~20 seconds, it prints the exact command to run by hand, e.g.:

```bash
cd ~/Projects/chess-coach && .venv/bin/python -m uvicorn chess_coach.api:app --host 127.0.0.1 --port 8787
```

Relay that command to the user rather than guessing at a fix.

## When Stockfish is missing

`GET /health` reports `engine: false` when Stockfish can't be reached.
If you see that, tell the user to install it and stop — do not try to
work around a missing engine by evaluating positions yourself:

```bash
brew install stockfish   # macOS
```

See `~/Projects/chess-coach/docs/SETUP.md` for the full setup, including
Linux install steps and the optional coach-chat gateway.
