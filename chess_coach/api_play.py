"""Phase 5+ — play endpoints. Live chess games against Stockfish.

Rewritten against docs/API.md §7.2 to fix ROADMAP §1.1-§1.3:

- §1.1 "Live games write junk mistake rows": mistakes are now built by the
  shared `classify.classify_move` funnel (Truth layer), never hand-rolled
  here with fabricated fields.
- §1.2 "Live games die on restart": every mutation mirrors the game into
  `live_state`; a cache miss rehydrates a `LiveGame` from that row instead
  of 404ing.
- §1.3 "One engine, two jobs": an `EnginePool` gives the opponent (fast,
  single line) and the analyst (deeper, multipv) their own process and
  lock, so a guard/hint check never has to wait behind — or corrupt — the
  opponent's reply.

Live games live in an in-process dict for the common case (no round trip
to SQLite on every ply) with `live_state` as the durable mirror.
"""
from __future__ import annotations

import secrets
import time
from typing import Optional

import chess
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .analysis import detect_motifs_before_move
from .classify import MoveVerdict, classify_move
from .coach_text import build_hint
from .engine import DEFAULT_DEPTH, EngineError, EngineLine
from .enginepool import LIVE_DEPTH, EnginePool
from .highlights import detect_highlight
from .journal import (
    Journal,
    insert_drill,
    insert_eval,
    insert_game,
    insert_guard_event,
    insert_highlight,
    insert_hint_event,
    insert_mistake,
    insert_position,
    load_live_state,
    save_live_state,
    utc_now_iso,
)
from .play import LiveGame, build_pgn, check_termination, parse_time_control, ply_of
from .strength import MIN_ELO, STOCKFISH_MAX_ELO


# A live game doesn't know its final length. `_phase_for_ply` wants a
# denominator to find "the last quarter of the game"; 80 plies (40 full
# moves) is a reasonable stand-in for "typical game length" until the
# game actually ends and analyze.py can use the real count.
LIVE_PHASE_ESTIMATE_PLIES = 80

# Exact hint-ladder cost table (task spec, not ROADMAP's draft numbers).
HINT_COSTS: dict[int, int] = {0: 0, 1: 1, 2: 1, 3: 2, 4: 2}
STARTING_HINT_CREDITS = 6

# Guard fires when a move drops >=300cp from a position that wasn't
# already lost/won beyond recognition (abs(eval_before) < 600).
GUARD_DELTA_THRESHOLD_CP = -300
GUARD_LIVE_POSITION_CAP_CP = 600


class NewGameRequest(BaseModel):
    user_color: str = "white"
    user_rating: Optional[int] = None
    engine_elo: int = 1500
    time_control: str = "15+10"


class ClockFull(BaseModel):
    white_ms: Optional[int]
    black_ms: Optional[int]
    increment_ms: Optional[int]


class ClockMoves(BaseModel):
    white_ms: Optional[int]
    black_ms: Optional[int]


class NewGameResponse(BaseModel):
    game_id: str
    fen: str
    user_color: str
    engine_color: str
    ply: int
    is_user_turn: bool
    clock: Optional[ClockFull]
    hint_credits: int
    # Echoed back because it is now load-bearing: it selects the opponent's
    # actual UCI settings (see `strength.py`). Before that it was decoration
    # and there was nothing worth confirming.
    engine_elo: int
    # Populated only when the engine moves first (user plays black), so the
    # client never has to brute-force which of the 20 legal first moves it
    # was.
    engine_move_san: Optional[str] = None
    engine_move_uci: Optional[str] = None


class MoveRequest(BaseModel):
    uci: str
    elapsed_ms: Optional[int] = None  # time the USER spent on this move


class FeedbackOut(BaseModel):
    severity: Optional[str]
    delta_cp: Optional[int]
    motifs: list[str]
    highlight: Optional[str]
    best_uci: Optional[str]
    phase: Optional[str]


class MoveResponse(BaseModel):
    fen: str
    user_move_san: str
    user_move_uci: str
    engine_move_san: Optional[str]
    engine_move_uci: Optional[str]
    eval_cp: Optional[int]
    eval_mate: Optional[int]
    ply: int
    is_user_turn: bool
    terminated: bool
    result: Optional[str]
    clock: Optional[ClockMoves]
    feedback: Optional[FeedbackOut]


class GuardRequest(BaseModel):
    uci: str


class GuardResponse(BaseModel):
    risky: bool
    delta_cp: Optional[int]


class PauseRequest(BaseModel):
    paused: bool


class PauseResponse(BaseModel):
    paused: bool
    clock: Optional[ClockMoves]


class HintRequest(BaseModel):
    tier: int


class HintResponse(BaseModel):
    tier: int
    credits_left: int
    cost: int
    text: str
    squares: list[str]
    motif: Optional[str]
    move_uci: Optional[str]
    pv: list[str]


class GameStateResponse(BaseModel):
    game_id: str
    fen: str
    ply: int
    is_user_turn: bool
    user_color: str
    terminated: bool
    result: Optional[str]
    move_history: list[str]
    clock: Optional[ClockFull]
    hint_credits: int
    paused: bool


# --- engine helpers -------------------------------------------------------


def _analyst_lines(
    pool: EnginePool, fen: str, *, multipv: int = 1, depth: int = DEFAULT_DEPTH,
) -> list[EngineLine]:
    """Analyst call that degrades to an empty list rather than raising.

    An offline/failed engine must not crash play — it just means feedback,
    highlights, and mistake rows go quiet for that move.
    """
    try:
        return pool.analyst_analyse(fen, depth=depth, multipv=multipv)
    except EngineError:
        return []


def _live_eval(pool: EnginePool, fen: str) -> Optional[EngineLine]:
    """The position's evaluation during a live game — always full strength.

    This has to be the *analyst*, never the opponent. The opponent engine
    is deliberately weakened to `lg.engine_elo` (see `strength.py`), and a
    handicapped engine's score is noise: Skill Level randomises its choice
    and its depth cap can be as low as 1. That number is persisted as the
    position's eval and feeds `mistakes`, ACPL and every downstream
    statistic, so taking it from the opponent would quietly corrupt the
    journal the weaker the opponent got. `LIVE_DEPTH` keeps it fast enough
    to sit inside a move response.
    """
    lines = _analyst_lines(pool, fen, multipv=1, depth=LIVE_DEPTH)
    return lines[0] if lines else None


def _persist_position(
    journal: Journal, lg: LiveGame, ply: int, fen: str,
    move_uci: str, move_san: str, clock_ms: Optional[int],
    line: Optional[EngineLine],
) -> int:
    """Insert one position row + (optionally) one eval row. Returns position id."""
    with journal.transaction() as conn:
        pos_id = insert_position(
            conn, game_id=lg.db_id, ply=ply, fen=fen,
            move_san=move_san, move_uci=move_uci, clock_ms=clock_ms,
        )
        if line is not None:
            insert_eval(
                conn, position_id=pos_id, engine="stockfish",
                depth=line.depth, cp=line.cp, mate=line.mate,
                best_uci=line.best_uci, pv=" ".join(line.pv), multipv_rank=1,
            )
    lg.last_pos_id = pos_id
    return pos_id


def _play_engine(
    pool: EnginePool, journal: Journal, lg: LiveGame,
) -> tuple[Optional[chess.Move], Optional[EngineLine], Optional[str]]:
    """Engine picks and pushes a move. Returns (move, post_eval_line, san)."""
    if lg.board.is_game_over():
        return None, None, None
    fen_before = lg.board.fen()
    # Two engines, two jobs: the weakened opponent decides *what to play*,
    # the full-strength analyst decides *what the position is worth*. They
    # used to be the same call, which is why weakening the opponent was
    # never safe before now.
    try:
        best_uci = pool.opponent_move(fen_before, elo=lg.engine_elo)
    except EngineError:
        return None, None, None
    if best_uci is None:
        return None, None, None
    try:
        em = chess.Move.from_uci(best_uci)
    except (ValueError, chess.InvalidMoveError):
        return None, None, None
    if em not in lg.board.legal_moves:
        return None, None, None
    ply = ply_of(lg.board)
    san = lg.board.san(em)
    pre = _live_eval(pool, fen_before)
    _persist_position(journal, lg, ply, fen_before, em.uci(), san, None, pre)
    lg.board.push(em)
    lg.pgn_so_far.append(san)
    post = _live_eval(pool, lg.board.fen())
    return em, post, san


def _finalize(journal: Journal, lg: LiveGame) -> None:
    """Write the PGN + result, and drop the live_state row (game is over)."""
    pgn_text = build_pgn(lg)
    with journal.transaction() as conn:
        conn.execute(
            "UPDATE games SET pgn = ?, result = ? WHERE id = ?",
            (pgn_text, lg.result or "draw", lg.db_id),
        )
        conn.execute("DELETE FROM live_state WHERE game_id = ?", (lg.db_id,))


def _mirror_live_state(journal: Journal, lg: LiveGame) -> None:
    """Write-through mirror so the game survives a server restart (ROADMAP §1.2)."""
    with journal.transaction() as conn:
        save_live_state(
            conn,
            game_id=lg.db_id,
            external_id=lg.game_id,
            user_color="white" if lg.user_color == chess.WHITE else "black",
            engine_elo=lg.engine_elo,
            fen=lg.board.fen(),
            moves_san=" ".join(lg.pgn_so_far),
            white_ms=lg.white_ms,
            black_ms=lg.black_ms,
            increment_ms=lg.increment_ms,
            hint_credits=lg.hint_credits,
            terminated=int(lg.terminated),
            result=lg.result,
            updated_at=utc_now_iso(),
        )


def _rehydrate(journal: Journal, row) -> LiveGame:
    """Rebuild a LiveGame from a `live_state` row by replaying its SAN history."""
    board = chess.Board()
    move_sans = [s for s in (row["moves_san"] or "").split(" ") if s]
    for san in move_sans:
        try:
            board.push_san(san)
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError,
                chess.AmbiguousMoveError):
            break
    user_color = chess.WHITE if row["user_color"] == "white" else chess.BLACK
    return LiveGame(
        game_id=row["external_id"],
        board=board,
        user_color=user_color,
        engine_color=not user_color,
        db_id=row["game_id"],
        started_at=time.time(),
        engine_elo=row["engine_elo"],
        pgn_so_far=move_sans,
        terminated=bool(row["terminated"]),
        result=row["result"],
        last_pos_id=0,
        white_ms=row["white_ms"],
        black_ms=row["black_ms"],
        increment_ms=row["increment_ms"] or 0,
        hint_credits=row["hint_credits"],
    )


def _get_live_game(journal: Journal, live_games: dict[str, LiveGame], game_id: str) -> LiveGame:
    lg = live_games.get(game_id)
    if lg is not None:
        return lg
    with journal.read() as conn:
        row = load_live_state(conn, external_id=game_id)
    if row is None:
        raise HTTPException(404, "no such live game")
    lg = _rehydrate(journal, row)
    live_games[game_id] = lg
    return lg


def _clock_for_mover(lg: LiveGame) -> Optional[int]:
    """Remaining ms for whoever's turn it is, or None for unlimited games."""
    if lg.white_ms is None:
        return None
    return lg.white_ms if lg.board.turn == chess.WHITE else lg.black_ms


def _apply_user_clock(lg: LiveGame, elapsed_ms: Optional[int]) -> tuple[bool, Optional[str], Optional[int]]:
    """Clamp `elapsed_ms` to [0, remaining], decrement, add increment.

    Returns (flagged, result_if_flagged, remaining_before_increment). The
    remaining-before-increment value is what feeds `classify_move`'s
    `clock_ms` (clock pressure at the moment of the move) and what gets
    persisted on the position row.
    """
    if lg.white_ms is None:  # unlimited game — no clock tracked at all
        return False, None, None
    is_white = lg.user_color == chess.WHITE
    remaining = lg.white_ms if is_white else lg.black_ms
    elapsed = 0 if elapsed_ms is None else elapsed_ms
    elapsed = max(0, min(elapsed, remaining))
    new_remaining = remaining - elapsed
    flagged = new_remaining <= 0
    if is_white:
        lg.white_ms = 0 if flagged else new_remaining + lg.increment_ms
    else:
        lg.black_ms = 0 if flagged else new_remaining + lg.increment_ms
    if flagged:
        return True, "loss", 0
    return False, None, new_remaining


def build_router(
    journal: Journal, pool: EnginePool, live_games: Optional[dict[str, LiveGame]] = None,
) -> APIRouter:
    """`live_games` may be shared with `api_coach.py`'s router (same dict
    instance) so a coach chat can ground itself in the live position
    without a second in-memory store. Callers that don't need that
    sharing (e.g. tests) can omit it and get a private dict.
    """
    if live_games is None:
        live_games = {}
    r = APIRouter()

    @r.post("/play/new", response_model=NewGameResponse)
    def play_new(req: NewGameRequest) -> NewGameResponse:
        if req.user_color not in ("white", "black"):
            raise HTTPException(400, "user_color must be white or black")
        if not MIN_ELO <= req.engine_elo <= STOCKFISH_MAX_ELO:
            raise HTTPException(
                400,
                f"engine_elo must be between {MIN_ELO} and {STOCKFISH_MAX_ELO}",
            )
        try:
            tc = parse_time_control(req.time_control)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        user_color = chess.WHITE if req.user_color == "white" else chess.BLACK
        game_id = secrets.token_urlsafe(12)
        with journal.transaction() as conn:
            db_id = insert_game(
                conn, source="local", external_id=game_id,
                played_at=time.strftime("%Y.%m.%dT%H:%M:%S+00:00", time.gmtime()),
                color=req.user_color, result="*", pgn="",
                my_rating=req.user_rating, opp_rating=req.engine_elo,
                time_control=req.time_control,
                eco="?", opening_name="?",
            )
        white_ms = black_ms = None
        increment_ms = 0
        if tc is not None:
            white_ms, increment_ms = tc
            black_ms = white_ms

        lg = LiveGame(
            game_id=game_id, board=chess.Board(),
            user_color=user_color, engine_color=not user_color,
            db_id=db_id, started_at=time.time(), engine_elo=req.engine_elo,
            user_rating=req.user_rating,
            white_ms=white_ms, black_ms=black_ms, increment_ms=increment_ms,
            hint_credits=STARTING_HINT_CREDITS,
        )
        live_games[game_id] = lg
        engine_move_san: Optional[str] = None
        engine_move_uci: Optional[str] = None
        if lg.board.turn != lg.user_color and not lg.board.is_game_over():
            em, _post_eval, em_san = _play_engine(pool, journal, lg)
            if em is not None:
                engine_move_san = em_san
                engine_move_uci = em.uci()
        _persist_position(journal, lg, 0, lg.board.fen(), "", "", None, None)
        _mirror_live_state(journal, lg)

        clock_out = None
        if white_ms is not None:
            clock_out = ClockFull(white_ms=lg.white_ms, black_ms=lg.black_ms, increment_ms=increment_ms)
        return NewGameResponse(
            game_id=game_id, fen=lg.board.fen(),
            user_color=req.user_color,
            engine_color="black" if req.user_color == "white" else "white",
            ply=ply_of(lg.board),
            is_user_turn=(lg.board.turn == user_color),
            clock=clock_out,
            hint_credits=lg.hint_credits,
            engine_elo=lg.engine_elo,
            engine_move_san=engine_move_san,
            engine_move_uci=engine_move_uci,
        )

    @r.post("/play/{game_id}/move", response_model=MoveResponse)
    def play_move(game_id: str, req: MoveRequest) -> MoveResponse:
        lg = _get_live_game(journal, live_games, game_id)
        if lg.terminated:
            raise HTTPException(400, "game already terminated")
        if lg.paused:
            raise HTTPException(409, "game is paused")
        if lg.board.turn != lg.user_color:
            raise HTTPException(400, "not your turn")
        try:
            user_move = chess.Move.from_uci(req.uci)
        except (ValueError, chess.InvalidMoveError, chess.AmbiguousMoveError):
            raise HTTPException(400, f"invalid UCI: {req.uci}")
        if user_move not in lg.board.legal_moves:
            raise HTTPException(400, f"illegal move: {req.uci}")

        user_move_san = lg.board.san(user_move)
        fen_before = lg.board.fen()
        ply_before = ply_of(lg.board)
        board_before = lg.board.copy()

        flagged, flag_result, mover_remaining = _apply_user_clock(lg, req.elapsed_ms)
        if flagged:
            lg.terminated = True
            lg.result = flag_result
            _finalize(journal, lg)
            clock_out = ClockMoves(white_ms=lg.white_ms, black_ms=lg.black_ms)
            return MoveResponse(
                fen=fen_before, user_move_san=user_move_san, user_move_uci=user_move.uci(),
                engine_move_san=None, engine_move_uci=None,
                eval_cp=None, eval_mate=None, ply=ply_before,
                is_user_turn=False, terminated=True, result=lg.result,
                clock=clock_out, feedback=None,
            )

        lines_before = _analyst_lines(pool, fen_before, multipv=3)
        eval_before_cp = lines_before[0].cp if lines_before else None
        best_uci_before = lines_before[0].best_uci if lines_before else None

        lg.board.push(user_move)
        lg.pgn_so_far.append(user_move_san)

        lines_after_user = _analyst_lines(pool, lg.board.fen(), multipv=1)
        eval_after_cp = lines_after_user[0].cp if lines_after_user else None

        verdict: Optional[MoveVerdict] = None
        if eval_before_cp is not None and eval_after_cp is not None:
            verdict = classify_move(
                board_before=board_before,
                played_move=user_move,
                eval_before_cp=eval_before_cp,
                eval_after_cp=eval_after_cp,
                best_uci=best_uci_before,
                ply=ply_before,
                total_plies=LIVE_PHASE_ESTIMATE_PLIES,
                my_rating=lg.user_rating,
                clock_ms=mover_remaining,
                out_of_book=False,
            )

        pos_id = _persist_position(
            journal, lg, ply_before, fen_before, user_move.uci(), user_move_san,
            mover_remaining, lines_before[0] if lines_before else None,
        )

        highlight_kind: Optional[str] = None
        if verdict is not None:
            highlight = detect_highlight(
                board_before=board_before,
                played_move=user_move,
                lines_before=lines_before,
                delta_cp=verdict.delta_cp,
                eval_before_cp=eval_before_cp,
                phase=verdict.phase,
                clock_ms=mover_remaining,
            )
            with journal.transaction() as conn:
                mistake_id: Optional[int] = None
                if verdict.severity is not None:
                    mistake_id = insert_mistake(
                        conn,
                        position_id=pos_id,
                        game_id=lg.db_id,
                        delta_cp=verdict.delta_cp,
                        severity=verdict.severity,
                        classification=verdict.classification,
                        phase=verdict.phase,
                        motifs=verdict.motifs,
                        engine_best_uci=verdict.engine_best_uci,
                        played_uci=verdict.played_uci,
                        instructive=verdict.instructive,
                        threshold_cp_in_force=verdict.threshold_cp_in_force,
                        rejection_reason=verdict.rejection_reason,
                    )
                    if verdict.instructive and mistake_id is not None:
                        insert_drill(
                            conn, mistake_id=mistake_id, fen=fen_before,
                            solution_uci=verdict.engine_best_uci,
                        )
                if highlight is not None:
                    kind, delta_to_2nd = highlight
                    highlight_kind = kind
                    insert_highlight(
                        conn, position_id=pos_id, game_id=lg.db_id, kind=kind,
                        ply=ply_before, played_uci=user_move.uci(),
                        delta_to_2nd=delta_to_2nd,
                    )

        engine_move_san: Optional[str] = None
        engine_move_uci: Optional[str] = None
        post_eval: Optional[EngineLine] = None

        terminated, result = check_termination(lg)
        if terminated:
            lg.terminated = True
            lg.result = result
        else:
            em, post_eval, em_san = _play_engine(pool, journal, lg)
            if em is not None:
                engine_move_san = em_san
                engine_move_uci = em.uci()
            terminated, result = check_termination(lg)
            if terminated:
                lg.terminated = True
                lg.result = result

        if lg.terminated and lg.result is not None:
            _finalize(journal, lg)
        else:
            _mirror_live_state(journal, lg)

        feedback = None
        if verdict is not None:
            feedback = FeedbackOut(
                severity=verdict.severity,
                delta_cp=verdict.delta_cp,
                motifs=verdict.motifs,
                highlight=highlight_kind,
                best_uci=verdict.engine_best_uci if lg.terminated else None,
                phase=verdict.phase,
            )

        clock_out = None
        if lg.white_ms is not None:
            clock_out = ClockMoves(white_ms=lg.white_ms, black_ms=lg.black_ms)

        return MoveResponse(
            fen=lg.board.fen(),
            user_move_san=user_move_san,
            user_move_uci=user_move.uci(),
            engine_move_san=engine_move_san,
            engine_move_uci=engine_move_uci,
            eval_cp=post_eval.cp if post_eval else None,
            eval_mate=post_eval.mate if post_eval else None,
            ply=ply_of(lg.board),
            is_user_turn=(not lg.terminated) and (lg.board.turn == lg.user_color),
            terminated=lg.terminated,
            result=lg.result,
            clock=clock_out,
            feedback=feedback,
        )

    @r.post("/play/{game_id}/guard", response_model=GuardResponse)
    def play_guard(game_id: str, req: GuardRequest) -> GuardResponse:
        """Pre-move risk check. Never mutates the live game, never leaks the fix."""
        lg = _get_live_game(journal, live_games, game_id)
        if lg.terminated:
            raise HTTPException(400, "game already terminated")
        if lg.paused:
            raise HTTPException(409, "game is paused")
        try:
            intended = chess.Move.from_uci(req.uci)
        except (ValueError, chess.InvalidMoveError, chess.AmbiguousMoveError):
            raise HTTPException(400, f"invalid UCI: {req.uci}")
        if intended not in lg.board.legal_moves:
            raise HTTPException(400, f"illegal move: {req.uci}")

        # Both evals run at LIVE_DEPTH, not DEFAULT_DEPTH. Two depth-18
        # searches sit between the user releasing a piece and the move being
        # sent, which is the single largest source of felt lag in the game —
        # and the number they produce is compared against a 300cp alarm
        # threshold, which does not need depth 18 to be right. LIVE_DEPTH is
        # also what `_live_eval` persists for the same position one request
        # later, so the guard and the journal now agree instead of scoring
        # the same position two different ways.
        fen_before = lg.board.fen()
        before_lines = _analyst_lines(pool, fen_before, multipv=1, depth=LIVE_DEPTH)
        eval_before_cp = before_lines[0].cp if before_lines else None

        scratch = lg.board.copy()
        scratch.push(intended)
        after_lines = _analyst_lines(pool, scratch.fen(), multipv=1, depth=LIVE_DEPTH)
        eval_after_cp = after_lines[0].cp if after_lines else None

        delta_cp: Optional[int] = None
        if eval_before_cp is not None and eval_after_cp is not None:
            delta_cp = -eval_after_cp - eval_before_cp

        risky = (
            delta_cp is not None
            and delta_cp <= GUARD_DELTA_THRESHOLD_CP
            and eval_before_cp is not None
            and abs(eval_before_cp) < GUARD_LIVE_POSITION_CAP_CP
        )

        if delta_cp is not None:
            with journal.transaction() as conn:
                insert_guard_event(
                    conn, game_id=lg.db_id, ply=ply_of(lg.board),
                    intended_uci=req.uci, delta_cp=delta_cp, overridden=False,
                )

        # Board is never touched: `scratch` is a copy, `lg.board` untouched.
        return GuardResponse(risky=bool(risky), delta_cp=delta_cp)

    @r.post("/play/{game_id}/hint", response_model=HintResponse)
    def play_hint(game_id: str, req: HintRequest) -> HintResponse:
        lg = _get_live_game(journal, live_games, game_id)
        if lg.paused:
            raise HTTPException(409, "game is paused")
        if req.tier not in HINT_COSTS:
            raise HTTPException(400, "tier must be 0-4")
        cost = HINT_COSTS[req.tier]
        if lg.hint_credits < cost:
            raise HTTPException(
                402,
                f"not enough hint credits: this tier costs {cost}, "
                f"you have {lg.hint_credits} left",
            )

        best_uci = ""
        motifs: list[str] = []
        pv: list[str] = []
        if req.tier > 0:
            lines = _analyst_lines(pool, lg.board.fen(), multipv=3)
            if not lines:
                raise HTTPException(503, "analysis engine unavailable")
            best_uci = lines[0].best_uci
            pv = lines[0].pv
            try:
                best_move = chess.Move.from_uci(best_uci)
                motifs = detect_motifs_before_move(lg.board.copy(), best_move, best_uci)
            except (ValueError, chess.InvalidMoveError):
                motifs = []

        content = build_hint(
            req.tier, board_before=lg.board.copy(), best_uci=best_uci or "0000",
            motifs=motifs, pv=pv, ply=ply_of(lg.board),
        )

        lg.hint_credits -= cost
        with journal.transaction() as conn:
            insert_hint_event(
                conn, game_id=lg.db_id, ply=ply_of(lg.board), tier=req.tier,
                credits_left=lg.hint_credits, motif=content.motif,
            )
            if req.tier == 4 and best_uci:
                # Tier 4 forces the position back as a drill (ROADMAP §2.1).
                # `drills` FKs to `mistakes`, so a synthetic mistake row
                # anchors it — see final report for the reasoning.
                mistake_id = insert_mistake(
                    conn,
                    position_id=lg.last_pos_id or 0,
                    game_id=lg.db_id,
                    delta_cp=0,
                    severity="mistake",
                    classification="tactical" if motifs else "positional",
                    phase="middlegame",
                    motifs=motifs,
                    engine_best_uci=best_uci,
                    played_uci=best_uci,
                    instructive=True,
                    threshold_cp_in_force=0,
                    rejection_reason=None,
                )
                insert_drill(
                    conn, mistake_id=mistake_id, fen=lg.board.fen(),
                    solution_uci=best_uci,
                )
        _mirror_live_state(journal, lg)

        return HintResponse(
            tier=req.tier, credits_left=lg.hint_credits, cost=cost,
            text=content.text, squares=content.squares, motif=content.motif,
            move_uci=content.move_uci, pv=content.pv,
        )

    @r.post("/play/{game_id}/resign")
    def play_resign(game_id: str) -> dict:
        lg = _get_live_game(journal, live_games, game_id)
        if not lg.terminated:
            lg.terminated = True
            lg.result = "loss"
            _finalize(journal, lg)
        return {"ok": True, "result": lg.result}

    @r.post("/play/{game_id}/pause", response_model=PauseResponse)
    def play_pause(game_id: str, req: PauseRequest) -> PauseResponse:
        """Stop or restart the game. The clock itself is client-reported
        (see `_apply_user_clock`), so pausing costs no time simply because
        the client stops counting — but the flag lives here so the server
        can refuse moves while it is set. A pause that only existed in the
        browser would be undone by a reload.
        """
        lg = _get_live_game(journal, live_games, game_id)
        if lg.terminated:
            raise HTTPException(400, "game already terminated")
        lg.paused = req.paused
        clock_out = None
        if lg.white_ms is not None:
            clock_out = ClockMoves(white_ms=lg.white_ms, black_ms=lg.black_ms)
        return PauseResponse(paused=lg.paused, clock=clock_out)

    @r.get("/play/{game_id}", response_model=GameStateResponse)
    def play_state(game_id: str) -> GameStateResponse:
        lg = _get_live_game(journal, live_games, game_id)
        clock_out = None
        if lg.white_ms is not None:
            clock_out = ClockFull(
                white_ms=lg.white_ms, black_ms=lg.black_ms, increment_ms=lg.increment_ms,
            )
        return GameStateResponse(
            game_id=lg.game_id,
            fen=lg.board.fen(),
            ply=ply_of(lg.board),
            is_user_turn=(not lg.terminated) and (lg.board.turn == lg.user_color),
            user_color="white" if lg.user_color == chess.WHITE else "black",
            terminated=lg.terminated,
            result=lg.result,
            move_history=lg.pgn_so_far,
            clock=clock_out,
            hint_credits=lg.hint_credits,
            paused=lg.paused,
        )

    return r


__all__ = [
    "build_router", "NewGameRequest", "NewGameResponse",
    "MoveRequest", "MoveResponse", "GameStateResponse",
    "GuardRequest", "GuardResponse", "HintRequest", "HintResponse",
    "PauseRequest", "PauseResponse",
]
