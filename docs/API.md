# Chess Coach — API & module contract

Single source of truth for the interfaces every module builds against.
Architecture rules are in [PROJECT.md](../PROJECT.md); the plan is in
[ROADMAP.md](../ROADMAP.md). This file says only *what the shapes are*.

Base URL: `http://127.0.0.1:8787`. Everything is JSON unless noted.

---

## 1. Layering (non-negotiable)

| Layer | Owns | Never does |
|---|---|---|
| Truth | `engine.py`, `analysis.py`, `classify.py`, `highlights.py`, python-chess | network, LLM |
| Data | `journal.py`, `api_*.py` | LLM, evaluation |
| Coach | `api_coach.py` → Hermes | evaluate a position |

`api_coach.py` is the ONLY module allowed to talk to Hermes. It sends
engine-derived facts and returns prose. It never asks "what should he play?".

---

## 2. Database — schema v3 (additive over v2)

`SCHEMA_VERSION = 3`. All new tables are `CREATE TABLE IF NOT EXISTS`
appended to `SCHEMA`; no table rebuild. The one column addition is guarded
by a `PRAGMA table_info` check because SQLite has no `ADD COLUMN IF NOT EXISTS`.

```sql
-- positive class: what the user did RIGHT (ROADMAP §3.2)
CREATE TABLE IF NOT EXISTS highlights (
    id            INTEGER PRIMARY KEY,
    position_id   INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN
                    ('only_move','found_tactic','resisted','converted','best_under_pressure')),
    delta_to_2nd  INTEGER,
    ply           INTEGER NOT NULL,
    played_uci    TEXT NOT NULL,
    note          TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_highlights_game ON highlights(game_id);

-- hint ladder purchases (ROADMAP §2.5)
CREATE TABLE IF NOT EXISTS hint_events (
    id            INTEGER PRIMARY KEY,
    game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply           INTEGER NOT NULL,
    tier          INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 4),
    motif         TEXT,
    credits_left  INTEGER NOT NULL,
    requested_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hint_events_game ON hint_events(game_id);

-- blunder guard firings (ROADMAP §2.2)
CREATE TABLE IF NOT EXISTS guard_events (
    id            INTEGER PRIMARY KEY,
    game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply           INTEGER NOT NULL,
    intended_uci  TEXT NOT NULL,
    delta_cp      INTEGER NOT NULL,
    overridden    INTEGER NOT NULL DEFAULT 0 CHECK (overridden IN (0,1)),
    fired_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guard_events_game ON guard_events(game_id);

-- the daily session (ROADMAP §7.4)
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY,
    day           TEXT NOT NULL UNIQUE,      -- YYYY-MM-DD local
    game_id       INTEGER REFERENCES games(id) ON DELETE SET NULL,
    drills_done   INTEGER NOT NULL DEFAULT 0,
    drills_total  INTEGER NOT NULL DEFAULT 0,
    reviewed      INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0,1)),
    started_at    TEXT NOT NULL,
    completed_at  TEXT
);

-- live game survives a server restart (ROADMAP §1.2)
CREATE TABLE IF NOT EXISTS live_state (
    game_id       INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    external_id   TEXT NOT NULL UNIQUE,
    user_color    TEXT NOT NULL CHECK (user_color IN ('white','black')),
    engine_elo    INTEGER NOT NULL,
    fen           TEXT NOT NULL,
    moves_san     TEXT NOT NULL DEFAULT '',   -- space-separated
    white_ms      INTEGER,
    black_ms      INTEGER,
    increment_ms  INTEGER NOT NULL DEFAULT 0,
    hint_credits  INTEGER NOT NULL DEFAULT 6,
    terminated    INTEGER NOT NULL DEFAULT 0 CHECK (terminated IN (0,1)),
    result        TEXT,
    updated_at    TEXT NOT NULL
);
```

Column addition, applied in `initialize()` only when missing:
```sql
ALTER TABLE games ADD COLUMN out_of_book_ply INTEGER;
```

### journal.py helpers to add

```python
def insert_highlight(conn, *, position_id: int, game_id: int, kind: str,
                     ply: int, played_uci: str,
                     delta_to_2nd: int | None = None,
                     note: str | None = None) -> int: ...

def list_highlights(conn, *, game_id: int) -> list[sqlite3.Row]: ...

def insert_hint_event(conn, *, game_id: int, ply: int, tier: int,
                      credits_left: int, motif: str | None = None) -> int: ...

def insert_guard_event(conn, *, game_id: int, ply: int, intended_uci: str,
                       delta_cp: int, overridden: bool = False) -> int: ...

def insert_drill(conn, *, mistake_id: int, fen: str, solution_uci: str,
                 due_at: str | None = None) -> int: ...
    # due_at defaults to utc_now_iso() (due immediately).
    # Idempotent: if a non-retired drill already exists for mistake_id,
    # return its id instead of inserting a duplicate.

def list_due_drills(conn, *, now_iso: str, limit: int = 8) -> list[sqlite3.Row]: ...
    # WHERE retired = 0 AND due_at <= now_iso, joined to mistakes for motifs
    # and to games for external_id. ORDER BY due_at ASC.

def insert_drill_attempt(conn, *, drill_id: int, correct: bool,
                         time_ms: int | None, moved_uci: str | None) -> int: ...

def update_drill_schedule(conn, *, drill_id: int, due_at: str,
                          interval_days: float, ease: float,
                          reps: int, lapses: int, retired: bool) -> None: ...

def insert_snapshot(conn, *, metric: str, value: float,
                    taken_at: str | None = None) -> int: ...

def get_or_create_session(conn, *, day: str) -> sqlite3.Row: ...
def update_session(conn, *, day: str, **fields) -> None: ...
def session_streak(conn, *, today: str) -> int: ...
    # consecutive days ending today (or yesterday) with a sessions row.

def save_live_state(conn, *, game_id: int, external_id: str, **fields) -> None: ...
def load_live_state(conn, *, external_id: str) -> sqlite3.Row | None: ...
def sweep_stale_live_games(conn, *, older_than_iso: str) -> int: ...
    # games with result='*' older than the cutoff -> result='draw',
    # their live_state rows deleted. Returns rows affected.
```

---

## 3. `chess_coach/classify.py` (NEW) — the shared funnel

Fixes ROADMAP §1.1. Both `analyze.py` and `api_play.py` call this; neither
builds a mistake row by hand any more.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class MoveVerdict:
    is_mistake: bool
    delta_cp: int
    severity: str | None                 # inaccuracy|mistake|blunder, None if below noise
    classification: str                  # tactical|positional|opening|endgame|time
    phase: str                           # opening|middlegame|endgame
    motifs: list[str]
    instructive: bool
    threshold_cp_in_force: int
    rejection_reason: str | None
    engine_best_uci: str
    played_uci: str

def classify_move(
    *,
    board_before: chess.Board,           # position BEFORE the played move
    played_move: chess.Move,
    eval_before_cp: int | None,          # side-to-move POV == user POV
    eval_after_cp: int | None,           # opponent POV (raw engine output after the move)
    best_uci: str | None,
    ply: int,
    total_plies: int,
    my_rating: int | None,
    clock_ms: int | None = None,
    out_of_book: bool = False,
) -> MoveVerdict | None: ...
```

Rules, in order:
1. Return `None` when either eval is `None` (nothing to say).
2. `delta_cp = (-eval_after_cp) - eval_before_cp` — both to user POV.
3. `severity = severity_from_delta(delta_cp)`; on `ValueError` return a
   `MoveVerdict` with `is_mistake=False` and `severity=None` (caller skips
   the row but may still record a highlight).
4. `phase = _phase_for_ply(ply, total_plies)` — **move this helper from
   `analyze.py` into `classify.py`** and have `analyze.py` import it, so
   there is one definition.
5. `motifs = detect_motifs_before_move(board_before.copy(), played_move, best_uci)`
6. `classification` — first match wins:
   - `"time"` if `clock_ms is not None and clock_ms < 30_000`
   - `"opening"` if `phase == "opening"` and `out_of_book`
   - `"endgame"` if `phase == "endgame"`
   - `"tactical"` if `motifs` is non-empty
   - else `"positional"`
   Must only ever return a value in `journal.VALID_CLASS`.
7. `verdict = evaluate_filter(...)` with `eval_before_cp`, `my_rating`,
   `best_uci or played_move.uci()`, `board_for_refutation=board_before.copy()`.

**Invariant, tested:** the same game analysed through `analyse_game` and
replayed move-by-move through `classify_move` produces identical
`(severity, classification, phase, motifs, instructive)` tuples.

---

## 4. `chess_coach/highlights.py` (NEW) — the positive class

```python
ONLY_MOVE_MARGIN_CP   = 150
FOUND_TACTIC_GAIN_CP  = 150
RESISTED_LOSS_CP      = 20
RESISTED_WORSE_CP     = -200
CONVERTED_FLOOR_CP    = 200
PRESSURE_MS           = 30_000

def detect_highlight(
    *,
    board_before: chess.Board,
    played_move: chess.Move,
    lines_before: list[EngineLine],   # multipv, rank 1..n, side-to-move POV
    delta_cp: int,
    eval_before_cp: int | None,
    phase: str,
    clock_ms: int | None,
) -> tuple[str, int | None] | None: ...
```

Returns `(kind, delta_to_2nd)` or `None`. First match wins, in this order:

| kind | condition |
|---|---|
| `only_move` | played == `lines_before[0].best_uci` AND `len(lines_before) >= 2` AND `lines_before[0].cp - lines_before[1].cp >= 150` |
| `found_tactic` | `delta_cp >= 150` AND `detect_motifs_after_move` finds a motif |
| `resisted` | `eval_before_cp <= -200` AND `delta_cp >= -20` |
| `converted` | `phase == "endgame"` AND `eval_before_cp >= 300` AND `delta_cp >= -20` |
| `best_under_pressure` | played == best AND `clock_ms is not None and clock_ms < 30_000` |

`cp` of `None` (mate lines) is treated as `+10_000 * sign(mate)` for
comparison purposes only — never stored.

---

## 5. `chess_coach/srs.py` (NEW) — the scheduler

SM-2 over the existing `drills` columns. No new dependency.

```python
GRADE_AGAIN, GRADE_HARD, GRADE_GOOD, GRADE_EASY = 0, 3, 4, 5
MIN_EASE = 1.3
RETIRE_AFTER_REPS = 5
RETIRE_MIN_INTERVAL_DAYS = 21.0

@dataclass(frozen=True)
class Schedule:
    due_at: str
    interval_days: float
    ease: float
    reps: int
    lapses: int
    retired: bool

def grade_from_attempt(correct: bool, time_ms: int | None) -> int: ...
    # wrong -> GRADE_AGAIN
    # correct, <  8000ms -> GRADE_EASY
    # correct, < 25000ms -> GRADE_GOOD
    # correct, otherwise -> GRADE_HARD

def next_schedule(*, grade: int, interval_days: float, ease: float,
                  reps: int, lapses: int, now: datetime) -> Schedule: ...
```

SM-2, explicitly:
- `grade < 3` → `reps = 0`, `lapses += 1`, `interval_days = 0.007` (~10 min),
  `ease = max(MIN_EASE, ease - 0.2)`, `retired = False`
- else → `reps += 1`;
  `interval_days = 1.0` if `reps == 1`, `6.0` if `reps == 2`,
  else `interval_days * ease`;
  `ease = max(MIN_EASE, ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)))`
- `retired = reps >= RETIRE_AFTER_REPS and interval_days >= RETIRE_MIN_INTERVAL_DAYS`
- `due_at = utc_now_iso()` of `now + timedelta(days=interval_days)`

---

## 6. Engine pool

`api.py` builds **two** engines (ROADMAP §1.3) and passes both down:

```python
@dataclass(frozen=True)
class EnginePool:
    opponent: StockfishEngine   # EngineOptions(multipv=1, threads=1, hash_mb=64)
    analyst:  StockfishEngine   # EngineOptions(multipv=3, threads=2, hash_mb=128)
```

Each engine gets its own `threading.Lock`; every `analyse` call goes through
`pool.opponent_analyse(...)` / `pool.analyst_analyse(...)` which hold the lock.
UCI is request/response and a shared process WILL interleave otherwise.
Live-play depth is `LIVE_DEPTH = 12` (fast); review depth stays `DEFAULT_DEPTH`.

---

## 7. HTTP API

### 7.1 Meta

```
GET /health -> {ok, schema_version, engine: bool, hermes: bool}
GET /       -> web/index.html (read fresh on every request)
/static/*   -> StaticFiles(directory="web")
```

### 7.2 Play — `api_play.py`

```
POST /play/new
  { user_color: "white"|"black", engine_elo: int, user_rating: int|null,
    time_control: "15+10"|"10+5"|"5+3"|"3+2"|"unlimited" }
  -> { game_id, fen, user_color, engine_color, ply, is_user_turn,
       clock: { white_ms, black_ms, increment_ms } | null,
       hint_credits: int }

POST /play/{game_id}/move
  { uci: str, elapsed_ms: int|null }        # time the USER spent on this move
  -> { fen, user_move_san, user_move_uci,
       engine_move_san|null, engine_move_uci|null,
       eval_cp|null, eval_mate|null, ply, is_user_turn, terminated, result|null,
       clock: { white_ms, black_ms } | null,
       feedback: {                          # null when there is nothing to say
         severity: str|null, delta_cp: int|null, motifs: [str],
         highlight: str|null, best_uci: str|null
       } | null }

POST /play/{game_id}/guard                   # ROADMAP §2.2 — before committing
  { uci: str }
  -> { risky: bool, delta_cp: int|null }
  # risky iff delta_cp <= -300 AND abs(eval_before) < 600.
  # Logs a guard_event. Does NOT mutate the game.

POST /play/{game_id}/hint                    # ROADMAP §2.1
  { tier: 0|1|2|3|4 }
  -> { tier, credits_left, cost, text,
       squares: [str], motif: str|null,
       move_uci: str|null, pv: [str] }       # move_uci/pv ONLY at tier 4
  # 402 when credits are insufficient. Tier 4 also creates a drill.

POST /play/{game_id}/resign  -> { ok, result }
GET  /play/{game_id}         -> full state incl. clock, hint_credits, move_history
```

**Tier text is generated locally, not by Hermes** — tiers 0–3 are template
strings over detector output. Only tier 4 may call the coach layer.

### 7.3 Review — `api_review.py` (extended)

```
GET /games                        (unchanged)
GET /games/{external_id}          (unchanged)
GET /mistakes                     (unchanged; FIX: MistakeSummary.ply must come
                                   from positions.ply, not position_id)

GET /review/{external_id}
  -> { game: {...}, moves: [ { ply, san, uci, fen, clock_ms, eval_cp, eval_mate,
                               best_uci, pv, is_user_move,
                               mistake: {...}|null, highlight: {...}|null } ],
       mistakes: [...], highlights: [...],
       accuracy: float, acpl: int }

GET /review/{external_id}/summary  -> { text, generated_at, grounded_on: {...}, source }
GET /review/{external_id}/pgn      -> text/plain, annotated (NAG + comment per mistake)
```

Accuracy: `100 * sum(1 for user moves with delta_cp > -50) / n_user_moves`,
rounded to 1dp. ACPL: mean of `max(0, -delta_cp)` over user moves.

### 7.4 Drills — `api_drills.py` (NEW)

```
GET  /drills/due?limit=8
  -> [ { id, fen, solution_uci, motif, severity, due_at, reps,
         game_external_id, ply, side_to_move } ]

POST /drills/{id}/attempt
  { moved_uci: str, time_ms: int|null }
  -> { correct, solution_uci, next_due_at, interval_days, reps, retired }

GET  /drills/stats
  -> { due_now, due_today, total, retired, retention_7d: float|null }
```

`retention_7d` = fraction of attempts in the last 7 days that were correct;
`null` when there are none. Never fabricate a number from zero attempts.

### 7.5 Session — `api_session.py` (NEW)

```
GET  /session/today   -> { day, drills_due, drills_done, drills_total,
                           game_id|null, game_external_id|null,
                           reviewed, streak, steps: [ {key, label, done} ] }
POST /session/today   -> same (creates the row if absent)
POST /session/today/complete { step: "drills"|"game"|"review" } -> same
```

Steps, in order: `drills`, `game`, `review`.

### 7.6 Stats — in `api_session.py`

```
GET /stats/profile
  -> { games_played, rating_estimate|null, acpl_overall|null,
       acpl_by_phase: {opening,middlegame,endgame},
       weakest_motifs: [ {motif, count} ],      # top 3 over last 20 games
       hints_per_game|null, guard_fire_rate|null,
       blunder_rate_under_30s|null, blunder_rate_over_120s|null,
       streak, drills_due }
GET /stats/snapshots?metric=acpl&limit=60 -> [ {taken_at, value} ]
```

Every field that has no data is `null`. **Never substitute 0 for "unknown".**

### 7.7 Coach — `api_coach.py` (NEW)

```
GET  /coach/status
  -> { available: bool, reason: str, base_url: str }

POST /coach/chat
  { messages: [ {role: "user"|"assistant", content: str} ],
    game_id: str|null }
  -> text/event-stream, lines of `data: {"delta": "..."} `,
     terminated by `data: [DONE]`.
```

Upstream: `HERMES_API_BASE` (default `http://127.0.0.1:8642/v1`), bearer
`HERMES_API_KEY` / `API_SERVER_KEY`, read from process env then `~/.hermes/.env`
via the existing loader pattern in `ingest.py::_load_token_from_env`.

**Security:** the upstream URL comes from env ONLY. The browser can never
supply, influence, or redirect it. No request body field is forwarded as a URL.

**Grounding:** `api_coach.py` builds a system message containing the current
FEN, the engine's eval and PV, detected motifs, and the user's recurring motif
counts — all read from the journal/engine — and appends the user's messages.
When `available` is false the endpoint returns a single SSE frame explaining
that the coach is offline and the local analysis still works. It never fakes
a reply.

---

## 8. Frontend — `web/`

No build step. ES modules, served statically. `index.html` is the shell only.

```
web/
├── index.html
├── css/
│   ├── tokens.css      design tokens, both themes
│   ├── base.css        reset, typography, layout primitives
│   ├── board.css       chessground skin (the study: walnut + paper)
│   └── app.css         panels, rail, chat, review, drills
└── js/
    ├── main.js         bootstrap + view router
    ├── api.js          fetch wrapper + SSE helper
    ├── state.js        single store, subscribe/notify
    ├── board.js        chessground lifecycle, dests, turnColor, arrows
    ├── play.js         play view: new game, moves, guard, termination
    ├── clock.js        dual clock, display only (server is authoritative)
    ├── rail.js         coach rail: process prompts, scan checklist, hint ladder
    ├── review.js       playback, move list, highlights, summary
    ├── scrubber.js     eval curve + time histogram + markers (the one canvas)
    ├── drills.js       due queue, attempt, feedback
    ├── chat.js         Hermes terminal panel (SSE)
    └── util.js         formatters (cp -> pawns, ms -> clock, san helpers)
```

Vendor, pinned, already proven to work in this project:
- `https://cdn.jsdelivr.net/npm/chess.js@0.10.3/chess.js` (classic script, global `Chess`)
- `https://cdn.jsdelivr.net/npm/chessground@9.1.1/dist/chessground.min.js` (ESM)

chessground facts that cost real debugging time — do not regress them:
- `movable.free = false` requires `movable.dests`, or every drag is silently refused.
- `isMovable()` needs `state.turnColor === piece.color`; chessground flips
  `turnColor` itself after a user move, and `configure()` reads only PIECES
  from a FEN. Always pass `turnColor` explicitly on every `board.set`.
- `board.set({fen: undefined})` is silently ignored — never pass a falsy FEN.
