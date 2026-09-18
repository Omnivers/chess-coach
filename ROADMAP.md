# Chess Coach — Roadmap: from "a board that works" to "a coach that teaches"

Companion to [PROJECT.md](PROJECT.md). PROJECT.md defines the architecture and the
guardrails and does not change. This document plans phases 7–13: the pedagogy, the
in-game coaching, the review experience, the openings, the clock, the visual design,
and the `/chess` daily on-ramp.

Written 2026-09-18. Status of the base: phases 0, 1, 3, 5 exist in some form;
phase 2 exists as detectors but is **not wired into live play**; phase 4 (drills)
is schema-only; phase 6 is blocked on the shared Telegram setup.

---

## 0. The one decision that governs everything else

**A coach that answers "what should I play?" during a game destroys the skill it
claims to build.**

Calculation is a muscle. Every time the agent supplies a move you were capable of
finding in 90 seconds, it performs the rep for you. You feel helped, you play better
that game, and you improve at nothing. This is the failure mode of every "AI chess
tutor" that ships — they are engines with a chat window bolted on.

So the rule, stated as sharply as §3's narration guardrail:

> **During a game the coach never names a move, a piece, or a square unless the user
> has spent the hint budget to buy it.** Before that, it may only ask questions and
> point at *process*.

Everything in §2 (the coach rail) is a consequence of this rule. Post-game (§3) the
rule lifts entirely — after the result is fixed, full disclosure is the whole point.

Corollary for a returning player specifically: your problem is almost certainly not
knowledge. You know what a fork is. Your problem is that you stopped *looking* on
every move. Retraining the look is a habit-formation task, not an information-transfer
task, and habit formation needs friction, not answers.

---

## 1. What has to be fixed before any of this lands

Four defects and gaps in the existing code block the pedagogy. None are large; all
are load-bearing.

### 1.1 Live games write junk mistake rows — **CRITICAL**

`chess_coach/api_play.py` (~line 250) persists every ≥100cp swing with:

```python
classification="tactical",   # hardcoded
phase="middlegame",          # hardcoded
motifs=[],                   # empty
instructive=False,           # always
threshold_cp_in_force=100,   # ignores instructive_threshold_cp(my_rating)
rejection_reason="live_game",
```

Meanwhile `chess_coach/analysis.py` already implements `detect_motifs_before_move`,
`detect_motifs_after_move`, `evaluate_filter`, and `instructive_threshold_cp` — and
`analyze.py` has `_phase_for_ply`. The live path simply does not call them.

Consequence: **every game you play in the app produces unusable training data.**
Motif recurrence — the central metric in PROJECT.md §7 — is permanently zero for
in-app games. Phase 4 drills built on these rows would drill nothing.

Fix: route the live-move path through the same funnel as `analyze.py`. One shared
`classify_mistake(board_before, move, before_eval, post_eval, ply, total_plies, rating)`
returning a full mistake record, called from both `analyze.py` and `api_play.py`.
Delete the duplicated inline logic. ~80 lines, plus tests pinning that a live game and
the same game replayed through `analyze.py` produce identical mistake rows.

### 1.2 Live games die on restart

`live_games: dict[str, LiveGame]` lives in a closure in `build_router`. Restart the
server mid-game and the game is gone — but the `games` row stays behind with
`result='*'` and an empty PGN, so the journal accumulates phantom games.

Fix: persist live state. The board is reconstructible from `positions` (fen + move_uci
per ply), so the only genuinely new state is `{user_color, engine_elo, terminated,
result, started_at, clock state}`. Add a `live_state` table keyed on `games.id`,
rehydrate on demand in `play_state`, and sweep `result='*'` games older than 24h into
`result='abandoned'` on startup. This is also the prerequisite for "resume the game of
the day" in §7.

### 1.3 One engine, two jobs

`create_app` builds a single `StockfishEngine` shared by the opponent and by analysis.
Under the coach rail (§2), a hint request and the opponent's reply can land in the same
second, and UCI is single-session: the second caller either blocks or corrupts the
first's `bestmove`.

Fix: an engine pool of two — `opponent` and `analyst` — with the analyst configured for
`multipv=3` (needed for "was there a second good move?" and for the hint ladder's
tier-2 category hints). Keep the pool in the app factory, one process still.

### 1.4 The frontend is a 404-line single file

`web/index.html` inlines markup, CSS, and an ESM `<script>` with chessground and
chess.js from CDN. It works — it now correctly handles `movable.dests`, `turnColor`
resync, and the no-game guard — but it cannot carry a coach rail, an eval scrubber,
a clock, a review mode, and an openings trainer.

It also still contains the debug `<pre id="debug">` panel and `log()` calls from the
"black isn't responding" investigation. That bug is fixed; the instrumentation should
go with it.

Fix: migrate to the Vite + React + TS stack PROJECT.md §8 already specifies, in one
deliberate move at the start of §6 — not incrementally. Serve the built `dist/` from
FastAPI at `/` so the "one command, one port" property survives.

---

## 2. Phase 7 — The coach rail (in-game help that doesn't cheat)

A narrow persistent column beside the board. It is *always* speaking, and it almost
never says anything specific.

### 2.1 The hint ladder

Five tiers. Each is a deliberate purchase, each is logged, and the cost is visible
before you click.

| Tier | Name | What it may say | Cost |
|---|---|---|---|
| **0** | Process prompt | Generic, position-blind. "Your opponent just moved. What does that piece attack now?" / "Before you move: checks, captures, threats." | Free, automatic |
| **1** | Temperature | "Something in this position is worth more than 2 minutes." Nothing about what or where. | Free, max 3/game |
| **2** | Category | "There is a tactic here for *you*." or "Your opponent has a threat." Names the motif class only: fork / pin / back rank / hanging piece. | 1 credit |
| **3** | Region | Highlights 2–4 squares that the motif involves, without saying which piece or which move. | 2 credits |
| **4** | Solution | The move, the PV, and Hermes's full explanation. | 3 credits — and the position is force-queued as a drill |

Credits: 6 per game, not replenished. Running out is information, not punishment.

Tier 4 auto-creating a drill is the key asymmetry: **taking the answer costs you the
position back on a schedule.** You will see it again in two days, unassisted.

### 2.2 The blunder guard — the single highest-value feature here

Before a move is sent to the server, the client asks the analyst engine whether the
move drops ≥300cp from a live position (`abs(eval_before) < 600`). If so, one modal:

> **Are you sure?** Take another look at this move.
> *[Play it anyway] [Let me look again]*

No reason given. No square named. It only says *stop and look*, which is precisely the
habit that decayed.

Settings: `off` / `once per game` / `always`. Default `once per game`. Every trigger is
logged whether or not you changed your mind — "blunders prevented by the guard" and,
more interestingly, "guard fired and I played it anyway" are both skill metrics.

This is not a takeback. The move has not been played. Takebacks are excluded from this
plan on purpose: a game you can rewind is not a game, and the pressure of irreversibility
is the thing being trained.

### 2.3 The passive rail

Between moves, without any request, the rail shows things that cost nothing to know:

- **Time budget** — "23 moves in, 8:40 left, ~22s/move on pace" (see §5)
- **Board scan checklist** — a three-item static list (checks / captures / threats) that
  you tick off; ticking is logged, and "moves played without completing the scan"
  correlates against blunder rate in your own data
- **Phase marker** — opening / middlegame / endgame, from `_phase_for_ply`
- **Opening name** while still in book, plus "out of book" the move you leave it (§4)

### 2.4 Layer discipline

Unchanged from PROJECT.md §3. The engine produces `{eval, best_uci, pv, multipv}`; the
detectors in `analysis.py` produce `{motif, squares}`; Hermes receives those as facts and
writes the sentence. Tiers 0 and 1 don't call Hermes at all — they're static strings and
a threshold. **Hermes is never asked "what should he play?"**

### 2.5 New schema

```sql
CREATE TABLE hint_events (
    id           INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply          INTEGER NOT NULL,
    tier         INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 4),
    motif        TEXT,
    credits_left INTEGER NOT NULL,
    requested_at TEXT NOT NULL
);

CREATE TABLE guard_events (
    id           INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply          INTEGER NOT NULL,
    intended_uci TEXT NOT NULL,
    delta_cp     INTEGER NOT NULL,
    overridden   INTEGER NOT NULL CHECK (overridden IN (0,1)),
    fired_at     TEXT NOT NULL
);
```

Both feed `skill_snapshots`: `hints_per_game`, `hint_tier_mean`, `guard_override_rate`.
Hint dependency falling over weeks is one of the clearest improvement signals available,
and it is not measurable anywhere else.

---

## 3. Phase 8 — The résumé: playback, and what was good

You asked for "resumé of parties with play back and what was good or not". This is the
half of the product that most tools get half-right: they show you what was bad.

### 3.1 Playback

- Move list with classification badges, keyboard-driven (`←`/`→` step, `↑`/`↓` jump
  between mistakes, `space` autoplay)
- Board with three arrow layers: **your move** (ink), **engine's move** (accent), **the
  threat you missed** (muted red) — drawn, not popped in
- Variation tree: step into the engine's line and keep playing it out against the
  analyst engine ("show me why that's better" — you play it, the engine answers)
- Eval graph across the bottom, doubling as the scrubber (§6.3)
- Per-move time overlay on the same graph — the clock and the eval share an x-axis, which
  is where the time-pressure story becomes visible without anyone writing a sentence

### 3.2 Classification — both directions

Mistakes already have `severity_from_delta`. Add the positive class:

```sql
CREATE TABLE highlights (
    id           INTEGER PRIMARY KEY,
    position_id  INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,     -- only_move | found_tactic | resisted | converted | best_under_pressure
    delta_to_2nd INTEGER,           -- how much better than the 2nd-best move
    note         TEXT,
    created_at   TEXT NOT NULL
);
```

Detection rules, all deterministic from multipv=3:

- **only_move** — you played the top move and it was ≥150cp better than the second best.
  You found the one thing that worked.
- **found_tactic** — your move triggered a motif detector in your favour and gained ≥150cp.
- **resisted** — you were worse than −200cp and your move lost ≤20cp. Defending well is a
  skill and nobody ever praises it.
- **converted** — winning endgame taken from ≥+300cp to a win without dropping below +200cp.
- **best_under_pressure** — top move with under 30s on the clock.

**Every review opens with the highlights, before the mistakes.** Not as a morale device —
as an accuracy correction. A review that lists only errors teaches you that you play badly,
which is both false and demotivating, and demotivation is the actual reason people stop
training. You will typically play 30 good moves and 2 bad ones; the report should say so.

### 3.3 The narrative summary

One Hermes-written paragraph per game, grounded exclusively in engine + detector output
and in **your history**. The history is the part no other tool has:

> "Accuracy 78%. Two mistakes, both the same shape: on move 14 and again on move 26 you
> moved a piece that was defending something else. That motif — *desperado defender* — is
> now 5 occurrences in 12 games, and every one of them happened with more than 3 minutes
> on your clock, so this isn't time pressure. Your opening was fine through move 11;
> you left book on 12.Bd3 where the main line plays 12.Bb3. Best moment: 19...Rd8, the
> only move that held, 220cp better than anything else."

The prompt to Hermes carries: FENs, played vs best UCI, PVs, deltas, detected motifs,
phase, clock deltas, highlight rows, and the motif recurrence counts from the journal.
It carries no request for evaluation. If the engine didn't say it, Hermes can't claim it.

### 3.4 Endpoints

```
GET  /review/{game_id}            full game, positions, evals, mistakes, highlights
GET  /review/{game_id}/summary    cached Hermes narrative (generated once, stored in coach_notes)
POST /review/{game_id}/explain    { ply } -> one explanation for one move, on demand
GET  /review/{game_id}/pgn        annotated PGN export ({ NAG + comment } per mistake)
```

Annotated PGN export matters: it's the escape hatch. Anything the journal holds should
leave in a format Lichess, SCID, or ChessBase can read. Never build a roach motel for
your own training data.

---

## 4. Phase 9 — Openings, taught backwards

### 4.1 The principle

Do not hand a returning player a repertoire to memorize. At your level the opening is
not what loses games — and memorized lines evaporate the first time someone deviates on
move 4, which is exactly when you most need to understand *why* the moves were the moves.

Teach openings **backwards from your own games**: ingest → find where you left book →
look at what actually happened in the next 10 moves → teach only the deviations that
cost you something.

### 4.2 Data

Lichess Opening Explorer, no key required:

```
GET https://explorer.lichess.ovh/lichess?variant=standard&fen={FEN}&speeds=blitz,rapid&ratings=1200,1400,1600
```

Returns move frequencies, win/draw/loss splits, and top games at a rating band. Two uses:

1. **Book boundary** — walk each game's plies until your move falls below a frequency
   floor at your band. That ply is `out_of_book_ply`, stored on `games`.
2. **Repertoire candidates** — rank openings by *what your opponents actually play against
   you*, not by what's objectively best.

Cache responses in a `book_positions` table keyed by FEN; the explorer is rate-limited and
your positions repeat heavily.

### 4.3 The recommendation engine

Opening choice should be prescribed from your **error profile**, not from taste:

| If your profile shows… | Prescribe | Why |
|---|---|---|
| Tactical blunders dominate | Open games — Italian, Scotch as White; 1...e5 as Black | You need volume of tactical positions to retrain pattern recognition. Hiding from tactics in a closed system defers the problem forever. |
| Blunders cluster under 30s (§5) | Systems — London, King's Indian Attack, Caro-Kann | Fewer branch points per game means fewer decisions means more clock where it matters. |
| Out of book before move 8 repeatedly | Narrow to one line and drill move-order | Breadth is the problem; depth is the fix. |
| Losses in equal endgames | Any — the opening isn't your problem | Prescribe §5 endgame curriculum instead and stop touching openings. |

**Concrete starting prescription, pending the ingest:** one opening as White, two as Black
(one vs 1.e4, one vs 1.d4). Three lines total. Not a repertoire — a foothold. Expand only
when recurrence data shows you're actually reaching move 12 in them.

### 4.4 Trainer mode

A play mode where the first N moves run against the book: you play your line, the engine
answers with the most common reply at your rating band, and any deviation stops the game
immediately with "the book plays X here — the point is Y" (point written by Hermes from
the explorer stats + the engine's eval of both moves). Deviations become drills.

---

## 5. Phase 10 — Time, and the curriculum

### 5.1 The clock

Currently absent: `positions.clock_ms` exists and the ingest reads Lichess clocks, but
in-app games write `clock_ms=None`, so half the analysis in PROJECT.md §1 can't run on
your own games.

Build: real dual clock, increment support, server-authoritative (the client displays, the
server decides — otherwise a slow round-trip steals your time), written to `clock_ms` on
every move.

Time controls offered, deliberately short list:

| Control | Label in UI | Purpose |
|---|---|---|
| **15+10** | *Rebuild* — **default** | Enough time to actually calculate. This is where skill returns. |
| 10+5 | Practice | Once the guard stops firing. |
| 5+3 | Test | Diagnostic only, once a week. |
| 3+2 | — | Available, with a warning: "Blitz measures what you already know. It doesn't teach." |

Defaulting to 15+10 is a pedagogical decision and should survive any UI redesign. A
returning player who plays blitz rebuilds nothing — they rehearse their bad habits faster.

### 5.2 Time coaching

- Live per-move pace in the rail (§2.3)
- Post-game: time spent vs. position criticality. The pathology to surface is **inverted
  time allocation** — 40s on a forced recapture, 4s on the only real decision of the game.
  Detectable: correlate `clock_ms` deltas against `delta_to_2nd` from multipv. Positions
  where many moves are near-equal deserve little time; positions with one good move deserve
  a lot. Spending the opposite way is the single most fixable time problem.
- `skill_snapshots` metrics: `mean_time_on_critical`, `mean_time_on_trivial`,
  `blunder_rate_under_30s` vs `blunder_rate_over_120s`.

### 5.3 Tutorials — a curriculum, not a course

A lesson is `{concept, 3–5 positions, a drill set, a check}`. Positions come from **your
games first**, classics second. A lesson is only unlocked when your error profile shows
you need it — the curriculum is generated, not authored.

The fixed spine for a returning player, in order:

1. **Board scan** — checks, captures, threats, every move, both colours. The single
   habit that prevents most of your losses.
2. **Hanging pieces** — yours and theirs. Includes fixing the known gap in `_is_hanging`
   (§8.2).
3. **The four forcing motifs** — fork, pin, skewer, discovery. Detectors already exist;
   the lesson is recognition speed, and the drill is timed.
4. **Basic endgames** — K+P vs K, opposition, Lucena, Philidor. Small, finite, and they
   convert games you're currently drawing or losing. Highest return per hour in all of chess.
5. **Piece activity over material** — when a pawn is worth giving.
6. **Opening principles** — centre, development, king safety. Not lines. §4 handles lines.
   The static primer content exists (`chess_coach/openings.py`, `GET /openings/primer`,
   `#/openings`) — five principles and four illustrative lines, hand-curated and
   engine-free. What's still missing is the lesson wrapper itself: this isn't wired into
   the FSRS queue or gated on error-profile need like the rest of §5.3, so it's a
   standalone reference page today, not yet a generated lesson.

Each lesson ends by inserting its positions into the FSRS queue. **Tutorials and drills
are the same system**; a lesson you don't get resurfaced is a lesson you didn't learn.

### 5.4 Phase 4 (FSRS) is the dependency for all of this

It is still schema-only. `drills` and `drill_attempts` exist; no scheduler, no queue, no
endpoints. Nothing in §5.3 works without it, and PROJECT.md §10 already flags phase 4 as
the point where improvement actually begins. **Build it before the tutorials, before the
openings, before the redesign.**

Minimum: FSRS-4 or SM-2 scheduler over the existing `interval_days/ease/reps/lapses`
columns, `GET /drills/due`, `POST /drills/{id}/attempt`, a queue UI, and drill creation
from both instructive mistakes and tier-4 hint purchases.

---

## 6. Phase 11 — Design

### 6.1 Direction: *the study*

Not a dashboard, not a dark-mode SaaS board, not Lichess-with-different-colours. The
reference is a **chess study room**: an annotated book on a desk, ink diagrams, a wooden
board, a clock. It is the right direction because it's what the product actually is —
you are reading commentary on your own games — and because it gives prose a natural home,
which board apps never have.

- **Light** (default): warm paper `oklch(96% 0.012 85)`, ink `oklch(22% 0.02 60)`,
  walnut board squares, a single blue accent reserved for engine output.
- **Dark**: walnut/tobacco ground, not grey. Cream text. Same accent.
- **Accent is semantic, never decorative.** Blue = the engine spoke. Red = material loss.
  Green = your good move. Nothing else gets colour, ever. When everything is coloured,
  the eval bar stops meaning anything.
- **Typography, two faces, paired on purpose:** a transitional serif for the coach's prose
  (the coach is writing you a letter, not emitting notifications), and a mono with real
  figure alignment for notation — move lists are tabular data and must line up in columns
  or they cannot be scanned.
- **Texture:** a very low-opacity paper grain on the page ground, and board squares with
  a wood texture rather than flat fills. Cheap, and it does most of the work of making the
  thing not look like a template.

### 6.2 Layout

Three zones, deliberately unequal — the board dominates, nothing competes with it:

```
┌──────────────────────────────────────────────────────────┐
│  Chess Coach          game of the day · 15+10    ⚙        │
├───────────────┬──────────────────────────┬───────────────┤
│  CLOCK  8:42  │                          │  COACH RAIL   │
│  ─────────    │                          │               │
│  opponent     │         BOARD            │  process      │
│               │        (dominant)        │  prompt       │
│  MOVE LIST    │                          │               │
│  1. e4   e5   │                          │  ☐ checks     │
│  2. Nf3  Nc6  │                          │  ☐ captures   │
│  3. Bc4  Nf6  │                          │  ☐ threats    │
│               │                          │               │
│  CLOCK 11:03  │                          │  hints  ●●●●○○│
│  you          │                          │  [ temperature]│
├───────────────┴──────────────────────────┴───────────────┤
│  ▁▂▃▅▆▅▃▂▁▁▂▇█▆▃  eval + time, and the scrubber          │
└──────────────────────────────────────────────────────────┘
```

### 6.3 The one grid-breaking element

**The eval graph is the scrubber.** A full-width strip under the board that is
simultaneously the evaluation curve, the per-move time histogram (inverted, beneath the
axis), the mistake markers, the highlight markers, and the thing you drag to move through
the game. One object, five jobs, and the time/eval correlation becomes visible rather
than narrated. In review mode it is the primary navigation; in play mode it grows
left-to-right as the game happens and is the only animated element on the screen.

### 6.4 Interaction and accessibility

- Keyboard-first: type SAN to move, `←`/`→` to scrub, `h` for a hint, `?` for shortcuts
- Arrows *draw* (stroke-dashoffset), annotations fade — nothing slides or bounces
- `prefers-reduced-motion` kills all of it and the product loses nothing
- Focus rings on the board grid; the board is navigable by arrow keys with a square cursor
- Contrast checked on both themes; the eval bar carries a number, never colour alone
- Responsive down to 768 (board over rail, scrubber persists); below that, review-only

### 6.5 Rebuild sequencing

Vite + React + TS + chessground, per PROJECT.md §8. Component tree:

```
src/
├── board/        Board.tsx  ArrowLayer.tsx  SquareCursor.tsx  board.css
├── rail/         CoachRail.tsx  HintLadder.tsx  ScanChecklist.tsx  BlunderGuard.tsx
├── clock/        Clock.tsx  TimeBudget.tsx
├── timeline/     EvalScrubber.tsx  TimeHistogram.tsx  MistakeMarkers.tsx
├── review/       ReviewPane.tsx  MoveList.tsx  VariationTree.tsx  Highlights.tsx
├── drills/       DrillQueue.tsx  DrillCard.tsx
├── openings/     BookStatus.tsx  TrainerMode.tsx
├── hooks/        useGame.ts  useEngineEval.ts  useClock.ts  useHints.ts
├── lib/          api.ts  fen.ts  eval.ts  san.ts
└── styles/       tokens.css  typography.css  global.css
```

FastAPI serves the built `dist/`. One command, one port, unchanged.

---

## 7. Phase 12 — `/chess`, and the game of the day

### 7.1 How the command actually works

Verified in `~/Projects/hermes-agent/agent/skill_commands.py`: Hermes registers a slash
command per skill, slugged from the SKILL.md frontmatter `name:` via
`slugify_skill_name()`. `name: chess-coach` is why `/chess-coach` exists today.

So `/chess` requires a **skill literally named `chess`**. Create a second, thin skill —
`~/.hermes/skills/games/chess/SKILL.md` with `name: chess` — that is purely a launcher.
Keep `chess-coach` as the deep skill (analysis, digests, weekly synthesis); `/chess` just
starts the session. Two skills, one repo, no ambiguity about which does what.

### 7.2 What "game of the day" means

Not "a random game". A fixed daily session with a defined shape:

```
/chess
  │
  ├─ 1. Health check       API up? Stockfish up? Start the server if not.
  ├─ 2. Warm-up            drills due today from FSRS (cap 8, ~4 min)
  ├─ 3. The game           one game, 15+10, vs Maia at your band, coach rail on
  ├─ 4. Review             immediate — highlights first, then mistakes
  └─ 5. Harvest            new drills queued; skill_snapshots written
```

That is the improvement loop, start to finish, in roughly 25 minutes. The point of the
command is that it is one word and the loop is not negotiable — habit beats intention.

`/chess review` jumps to the last game. `/chess drills` runs step 2 alone. `/chess stats`
prints the snapshot trend. Bare `/chess` runs the whole thing.

### 7.3 The script

`~/.hermes/skills/games/chess/scripts/chess_day.py`, symlinked to
`~/Projects/chess-coach/scripts/chess_day.py` (the Coin Scout pattern):

```python
# 1. ensure the API is up (spawn uvicorn detached if /health fails)
# 2. GET /drills/due            -> today's warm-up
# 3. GET /stats/profile         -> current band, weakest motif, streak
# 4. POST /session/new          -> creates the day's session, returns session_id
# 5. open http://127.0.0.1:8787/#/session/{id}
# 6. print a compact JSON block for Hermes to narrate
```

Hermes narrates the JSON. It does not compute any of it. Same guardrail, same reason.

### 7.4 Session persistence

`/chess` twice in one day resumes rather than restarts — which requires §1.2 (live game
persistence). Add:

```sql
CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    day          TEXT NOT NULL UNIQUE,      -- YYYY-MM-DD, local
    game_id      INTEGER REFERENCES games(id),
    drills_done  INTEGER NOT NULL DEFAULT 0,
    drills_total INTEGER NOT NULL DEFAULT 0,
    reviewed     INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0,1)),
    started_at   TEXT NOT NULL,
    completed_at TEXT
);
```

A visible streak counter over `sessions` is the only gamification in this plan. One
number, honest, and it's the one that correlates with actually improving.

### 7.5 Phase 6 lands here too

The morning Telegram digest from PROJECT.md §10 is the same data as §7.2 step 1, pushed
instead of pulled: *"5 drills due, last game Tuesday, knight forks now 6 occurrences in
14 games. /chess"*. Still blocked on the shared `TELEGRAM_BOT_TOKEN` setup that Coin
Scout is also waiting on — one setup, two agents.

---

## 8. Opponent, and one known correctness gap

### 8.1 Maia

PROJECT.md §9 settles this: Maia over weakened Stockfish, because
`UCI_LimitStrength` produces strong play punctuated by random garbage, and the patterns
you learn to punish never occur in human games. `lc0` is installed; the weights are not.

```bash
# maia1100 / maia1500 / maia1900 from CSSLab/maia-chess on HuggingFace
# -> ~/.local/share/maia/maia-1500.pb.gz, lc0 --weights=...
```

Add a `MaiaEngine` alongside `StockfishEngine` behind a common `Opponent` protocol; band
selected from your current rating, one step above it. Stockfish stays as the analyst —
truth layer is unchanged and is never Maia.

### 8.2 `_is_hanging` is too narrow

`chess_coach/analysis.py:186` — a piece is hanging when `attackers > 0 and defenders == 0`.
That misses **insufficiently defended**: a knight on a square attacked twice and defended
once is lost material, and that is the more common amateur blunder by a wide margin.

The fix is static exchange evaluation, and it is deliberately deferred in PROJECT.md to
phase 2 — but the lesson in §5.3 item 2 depends on it, so it must land before the
tutorials. Implement SEE, keep the existing behaviour as a special case, and add
regression tests alongside the king-as-target tests in `tests/test_motifs.py` (the
invariant in PROJECT.md §6 stays exactly as it is).

---

## 9. Order of work

Ranked by learning value per unit of effort, not by how interesting it is to build.

| # | Work | Why here | Size |
|---|---|---|---|
| 1 | §1.1 live-game mistake classification | Everything downstream trains on this data. It is currently junk. | S |
| 2 | §5.4 FSRS drill loop | PROJECT.md §10: improvement starts here. Nothing else substitutes. | M |
| 3 | §7 `/chess` + session | Cheap, and it's the daily on-ramp. Habit early beats features early. | S |
| 4 | §5.1 clock | Small, unlocks time analysis already designed and half-built. | S |
| 5 | §3 review + playback + highlights | Closes the loop; the thing you asked for most directly. | M |
| 6 | §2 coach rail + blunder guard | Highest in-game value. Needs §1.3 engine pool first. | M |
| 7 | §8.2 SEE | Correctness gap that gates lesson 2. | S |
| 8 | §6 frontend rebuild | Do it once the feature set is known — but §2 and §3 will fight the single file. | L |
| 9 | §4 openings | Genuinely useful, genuinely not your bottleneck yet. | M |
| 10 | §5.3 tutorials | Largest, and depends on 2, 5, and 7. | L |
| 11 | §8.1 Maia | Quality-of-opponent upgrade, not a blocker. | S |

Items 1–4 are roughly a weekend and take the product from "a board that works" to "a loop
that teaches". Items 5–7 are the second weekend and are where it starts to feel like a
coach. 8–11 are the long tail.

**Still blocked on you:** the Lichess username (§PROJECT.md 12) — without the back
catalogue the error profile is empty, the opening recommendation in §4.3 has nothing to
read, and the first two weeks of drills are guesswork.

---

## 10. How we'll know it worked

Improvement is a number that moves, or it didn't happen (PROJECT.md §7). The numbers:

| Metric | Source | Direction |
|---|---|---|
| ACPL by phase | `skill_snapshots` | down |
| Motif recurrence rate | `mistakes.motifs` over trailing 20 games | down per motif |
| Drill retention at 7 days | `drill_attempts` | up |
| Hints per game / mean tier | `hint_events` | down |
| Guard fire rate | `guard_events` | down |
| Guard override rate | `guard_events.overridden` | down (overriding the guard and being wrong is the worst signal in the set) |
| Blunder rate under 30s vs over 120s | `positions.clock_ms` | gap narrowing |
| Time on critical vs trivial positions | clock × `delta_to_2nd` | inverting to correct |
| Out-of-book ply | `games.out_of_book_ply` | up, slowly |
| Session streak | `sessions` | up |

If six months in the hints are gone, the guard has stopped firing, and the drill retention
is above 80%, the coach worked. If the numbers are flat, the coach didn't, and no amount
of pleasant narration should be allowed to obscure that.
