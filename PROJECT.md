# Chess Coach — localhost training platform

**Goal:** rebuild lost chess skill through a coach that learns *my* failure patterns,
resurfaces them on a schedule, and gets sharper as I do.

Status: PLAN (2026-09-17). Nothing built yet.

---

## 1. The thesis

Engine analysis tells you **what** was better. It never tells you **why you didn't see it**.
That gap is the entire product. Lichess already gives free Stockfish analysis — if that
were sufficient, everyone using it would improve, and they don't.

Three things engines don't do, that this will:

1. **Classify errors as recurring cognitive patterns**, not centipawn loss. "Hung a piece
   to a knight fork", "missed opponent's threat", "traded into a lost endgame" are
   different diseases with different cures. Raw ACPL treats them as one number.
2. **Spaced repetition on my own mistakes.** Chessable does SRS on curated openings.
   Lichess shows a mistake once and forgets it. Neither reschedules *my* blunder position
   three days later to see whether I actually learned it. This is the highest-leverage
   mechanic in the whole plan.
3. **Filter for what's learnable at my level.** A 0.3 → 0.1 "inaccuracy" is noise below
   2000. Most tools flood you with them. A mistake earns a drill only if it passes the
   instructive filter (§5).

**Clock cross-reference** deserves its own mention: every position stores `clock_ms`. If
blunders cluster under 30 seconds remaining, the problem is time management, not tactics —
and no amount of puzzle-solving fixes it. Only a journal surfaces that.

---

## 2. Architecture — three layers, strictly separated

| Layer | Component | Rule |
|---|---|---|
| **Truth** | Stockfish (native, UCI) + python-chess | Never an LLM. Evaluations and legality come from here or nowhere. |
| **Data** | SQLite journal + thin FastAPI service on `:8787` | Deterministic, no LLM, sub-ms reads. Everything the UI *renders*. |
| **Coach** | Hermes via `POST /v1/runs` + SSE `/v1/runs/{id}/events` | Narration, conversation, weekly synthesis, cron digests. Everything the UI *says*. |

The frontend hits **layer 2 for what it draws** and **layer 3 for what it explains**.
Rendering a drill queue through an LLM would be slow, costly, and nondeterministic;
explaining a blunder through SQL is impossible. The split is not negotiable.

### Hermes as backend — what's actually there

Verified in `~/Projects/hermes-agent/gateway/platforms/api_server.py`:

```
POST /v1/runs                     start an agent run
GET  /v1/runs/{run_id}/events     SSE event stream
POST /v1/runs/{run_id}/steer      mid-run correction
POST /v1/runs/{run_id}/stop
POST /v1/chat/completions         OpenAI-compatible, streaming
GET  /v1/skills  /v1/toolsets
```

This is a real HTTP backend — Hermes genuinely leads. What it does *not* provide is a
chess-shaped data API (games, evals, drills). That's layer 2, and it's ~150 lines.

Hermes reaches the journal the same way Coin Scout does: **skill scripts** under
`~/.hermes/skills/games/chess-coach/scripts/`, symlinked to the project.

---

## 3. The guardrail (biggest failure risk in the project)

> **Hermes never evaluates a position.**
> Hermes receives `{FEN, my move, engine best line + PV, eval delta, detected motif}` and
> writes the *explanation*. If the engine didn't say it, Hermes doesn't claim it.
> Every stored explanation carries the engine evidence that grounds it, so it's auditable.

LLMs are weak at unaided board reasoning. Unconstrained, they produce fluent, confident,
wrong chess — which is worse than no coach, because it's persuasive. Engine is ground
truth; Hermes is the narrator.

---

## 4. Journal schema (Phase 0 — built first, same lesson as Coin Scout)

```
games           id, source(lichess|chesscom|local|otb), external_id, played_at, color,
                result, time_control, my_rating, opp_rating, eco, opening_name, pgn
positions       id, game_id, ply, fen, move_san, move_uci, clock_ms
evals           position_id, engine, depth, nodes, cp, mate, best_uci, pv,
                multipv_rank, evaluated_at
mistakes        id, position_id, game_id, delta_cp, severity, class, phase, motifs,
                engine_best_uci, played_uci, instructive, threshold_cp_in_force,
                explanation, explained_at
drills          id, mistake_id, fen, solution_uci, created_at,
                due_at, interval_days, ease, reps, lapses        -- FSRS state
drill_attempts  drill_id, attempted_at, correct, time_ms, moved_uci
skill_snapshots taken_at, metric, value
coach_notes     created_at, scope, note
```

`threshold_cp_in_force` is the Coin Scout lesson carried over: **store the continuous
value AND the threshold that was active**, never a bare pass/fail. When the bar moves as I
improve, old rows stay interpretable.

---

## 5. The instructive filter

A mistake enters the drill queue only if **all three** hold:

1. `delta_cp >= threshold(my_rating)` — roughly 150cp at 1200, 80cp at 1800
2. **Findable** — refutation is a short forcing line (≤ N plies, checks/captures/threats)
3. **Live** — `abs(eval_before) < 600cp`; errors in already-won or already-lost positions
   teach nothing

Everything rejected is still logged. Rejected mistakes are the negative class if I ever
want to fit the filter empirically instead of guessing at it.

---

## 6. Error taxonomy

- **Tactical** — hung piece, missed fork/pin/skewer/discovery, back-rank, overlooked
  opponent threat, missed own tactic, calculation truncated too early
- **Positional** — bad trade, structure damage, bad bishop, square concession,
  misplaced piece, premature attack
- **Opening** — out of book early, known trap, move-order error
- **Endgame** — technique loss, king activity, opposition, pawn-race miscount
- **Time** — blunder under clock pressure (cross-referenced from `clock_ms`)

**Invariant — the king is a tactical target, not an exception.** Fork and skewer
detection must rank the king *highest*, because check is forcing: that is exactly what
makes a royal fork win material. Excluding the king (tempting, since it can't be captured)
silently deletes the most common and most instructive tactics in amateur chess — knight
forks king+queen, rook skewers king-then-queen. `_piece_value` keeps king=0 for *material*
counting; `_target_value` ranks it above the queen for *tactical* targeting. Two different
questions, two different functions. Regression tests in `tests/test_motifs.py` pin this.

Several of these are **deterministically detectable** with python-chess — a hung piece is
an undefended piece that's capturable; a fork is one piece attacking two of higher value;
back-rank is checkable from king mobility. Detect what's detectable; let Hermes narrate
the rest. Don't ask an LLM to do arithmetic a board library already does.

---

## 7. "Improves while I improve" — concrete meaning

Not a vibe. Four mechanisms:

1. `skill_snapshots` tracks ACPL by phase, motif recurrence rate, drill retention,
   blunder-under-time-pressure rate
2. The instructive threshold **auto-raises** as ACPL falls — the bar tracks my level
3. Retired motifs stop being drilled; recurring motifs get weighted up in the queue
4. `coach_notes` — Hermes's versioned model of me, rewritten weekly, and readable by me

Improvement is a number that moves, or it didn't happen.

---

## 8. Frontend — open source, assembled not written

| Repo | License | Role |
|---|---|---|
| `lichess-org/chessground` | GPL-3.0 | The board. What Lichess itself ships. ~30-line React wrapper. |
| `jhlywa/chess.js` | BSD-2 | Legality, PGN, FEN |
| `lichess-org/lichess-pgn-viewer` | GPL-3.0 | Review pane with variation tree (optional) |
| `Clariity/react-chessboard` | MIT | Fast-path alternative if the wrapper isn't worth it |

Vite + React + TS. Licensing is moot on localhost — nothing is distributed.

---

## 9. Opponent — Maia, not weakened Stockfish

Stockfish at `UCI_LimitStrength` plays strong moves punctuated by random garbage. Nothing
about it resembles a human, so the patterns you learn to punish never appear in real games.

**Maia** (`CSSLab/maia-chess`) is trained on human games at a rating band and reproduces
*human* errors. Weights `maia1100` / `maia1500` / `maia1900` on HuggingFace, runs under lc0.
Train against the mistakes you'll actually face.

---

## 10. Phases

| Phase | Deliverable | Gate |
|---|---|---|
| **0** | Journal schema, models, tests. No UI, no network. | tests green |
| **1** | Lichess/Chess.com ingest → PGN → positions; Stockfish multipv batch analysis. CLI only. | real games analyzed end to end |
| **2** | Taxonomy + instructive filter + deterministic motif detection + Hermes narration | explanations grounded in engine PV |
| **3** | Review UI — game list, board, eval bar, mistake markers, my-move-vs-best | usable without the terminal |
| **4** | **SRS drill loop (FSRS).** Daily queue from my own errors. | ← improvement actually starts here |
| **5** | Play vs Maia in-browser; games auto-journal | a game played in-app appears in review |
| **6** | Hermes cron + Telegram morning digest | "3 games, 2 mistakes, same motif as last week, 5 drills due" |

Journal before UI — the OmniTrade lesson. A pretty board over no feedback loop is the
same failure in a different costume.

Phase 6 reuses the **exact Telegram gateway setup already pending for Coin Scout**.
One setup, two agents.

---

## 11. Prerequisites

```
brew install stockfish        # not installed
brew install lc0              # not installed — for Maia
pip install chess             # python-chess, not installed
```
Node v25.8.2 present. Use a 3.14 venv (system python3 is 3.9.6).
The Hermes `api_server` platform must be enabled in the gateway config.

---

## 12. Decisions settled (2026-09-17)

**Source: Lichess.** **Scope: analyze first, play later** — phases run 0→6 as ordered.

### Why Lichess is the good case

`GET https://lichess.org/api/games/user/{username}` streams the whole archive, with three
parameters that matter a lot:

| Param | Why it matters |
|---|---|
| `clocks=true` | Per-move clock times. The time-pressure analysis (§1) comes free — no inference needed. |
| `evals=true` | Lichess's own server analysis, where the game already has it. Stockfish then only runs on unanalysed games and on deeper multipv where it counts. Saves hours of CPU on a back-catalogue import. |
| `since=<ms>` | Incremental pull. Cron fetches only games since last ingest — Phase 6 becomes trivial. |

Accepts `application/x-ndjson` for structured rows instead of raw PGN. Unauthenticated
works but is rate-limited; a personal token from `lichess.org/account/oauth/token`
(scope: none needed for public games) raises the ceiling and is worth having for the
initial bulk import.

**Consequence for Phase 1:** the back-catalogue is the cold start. Importing the full
history means the coach has a real error profile on day one, instead of waiting weeks to
accumulate one game at a time. Motif recurrence — the central metric — needs volume before
it says anything.

### Still needed before Phase 1

- Lichess username
- Optionally a personal API token for the bulk import
