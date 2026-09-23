"""Phase 6 — the Hermes proxy. The ONLY module allowed to talk to an LLM.

Layer discipline (PROJECT.md §3, API.md §1): every other module in this
app is Truth (evaluates positions, never a network call) or Data (reads/
writes the journal, never evaluates). This module is Coach: it turns
engine-derived facts plus the user's own words into prose, and never the
reverse — a model's reply here is display-only, never parsed back into a
move or acted on.

SSRF boundary (non-negotiable): the upstream base URL comes from the
`HERMES_API_BASE` environment variable ONLY. No request body field is
ever read into a URL, appended to one, or used to redirect one. The
bearer token is read from `HERMES_API_KEY`/`API_SERVER_KEY` (process env,
then `~/.hermes/.env`, mirroring the exact lookup order already used by
`ingest.py::_load_token_from_env`) and is never logged, echoed, or placed
in an error message — only ever sent in the outbound Authorization header.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import chess
import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .analysis import detect_motifs_before_move
from .coach_text import local_review_summary
from .engine import DEFAULT_DEPTH
from .enginepool import EnginePool
from .journal import Journal, utc_now_iso
from .play import LiveGame

DEFAULT_BASE_URL = "http://127.0.0.1:8642/v1"
STATUS_CACHE_TTL_S = 30.0
AVAILABILITY_TIMEOUT_S = 1.5
CHAT_TIMEOUT_S = 30.0


def _base_url() -> str:
    """Upstream base URL — env only. Never derived from a request."""
    return os.environ.get("HERMES_API_BASE", DEFAULT_BASE_URL)


def _load_token() -> Optional[str]:
    """Bearer token: process env (HERMES_API_KEY, then API_SERVER_KEY),
    else the same two keys in `~/.hermes/.env`. Same lookup pattern as
    `ingest.py::_load_token_from_env`, generalized to two candidate keys.
    """
    for key in ("HERMES_API_KEY", "API_SERVER_KEY"):
        val = os.environ.get(key)
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
        if key.strip() in ("HERMES_API_KEY", "API_SERVER_KEY"):
            return value.strip().strip('"').strip("'")
    return None


_status_cache: dict = {"checked_at": 0.0, "available": False, "reason": "not checked yet"}


async def _check_availability() -> tuple[bool, str]:
    """A real, cheap reachability probe — cached for STATUS_CACHE_TTL_S.

    The gateway is very likely not running in most environments; that is
    an entirely normal, honestly-reported state, not an error.
    """
    now = time.monotonic()
    if now - _status_cache["checked_at"] < STATUS_CACHE_TTL_S:
        return _status_cache["available"], _status_cache["reason"]

    token = _load_token()
    if token is None:
        available, reason = False, "no Hermes API key configured (set HERMES_API_KEY or API_SERVER_KEY)"
    else:
        base = _base_url()
        try:
            async with httpx.AsyncClient(timeout=AVAILABILITY_TIMEOUT_S) as client:
                resp = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {token}"})
            if resp.status_code < 400:
                available, reason = True, "gateway reachable"
            else:
                available, reason = False, f"gateway responded with HTTP {resp.status_code}"
        except httpx.HTTPError:
            available, reason = False, "gateway not reachable (connection failed or timed out)"

    _status_cache["checked_at"] = now
    _status_cache["available"] = available
    _status_cache["reason"] = reason
    return available, reason


class CoachStatus(BaseModel):
    available: bool
    reason: str
    base_url: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    game_id: Optional[str] = None
    # 'fr' | 'en'; anything else falls back to English rather than raising,
    # since a stray value here is a frontend bug, not grounds to break chat.
    lang: str = "fr"
    # Current view ('play'|'review'|'drills'|'openings'|'study'|...), so the
    # briefing can tell the model where the student actually is instead of
    # guessing from the conversation text.
    route: Optional[str] = None


def _top_recurring_motifs(journal: Journal, limit: int = 3) -> list[tuple[str, int]]:
    """The user's most common motifs across instructive mistakes, for grounding."""
    with journal.read() as conn:
        rows = conn.execute(
            "SELECT motifs FROM mistakes WHERE instructive = 1 AND motifs IS NOT NULL AND motifs != ''"
        ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for m in (r["motifs"] or "").split(","):
            m = m.strip()
            if m:
                counts[m] = counts.get(m, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def _record_summary(journal: Journal) -> dict:
    """Win/loss/draw/unfinished totals over every logged game, one pass.

    '*' games are bucketed as `unfinished` rather than folded into losses:
    an abandoned game or one still being played on the source site is not
    a defeat, and the briefing must not let the model editorialize about a
    result that was never actually recorded.
    """
    with journal.read() as conn:
        rows = conn.execute("SELECT result, played_at FROM games").fetchall()
    return {
        "total": len(rows),
        "wins": sum(1 for r in rows if r["result"] == "win"),
        "losses": sum(1 for r in rows if r["result"] == "loss"),
        "draws": sum(1 for r in rows if r["result"] == "draw"),
        "unfinished": sum(1 for r in rows if r["result"] == "*"),
        "last_played_at": max((r["played_at"] for r in rows), default=None),
    }


def _recent_games(journal: Journal, limit: int = 5) -> list[dict]:
    """Recent games, the ones that actually finished first.

    Ordering purely by date buries every finished game behind abandoned '*'
    stubs once a journal accumulates them (a board opened and walked away
    from logs a row just like a real game does). Finished games are the only
    ones carrying analysis the coach can say anything useful about, so they
    take the slots first; whatever is left over is filled with the most
    recent unfinished ones, so the list still tells the truth about what the
    student has been doing lately.
    """
    with journal.read() as conn:
        rows = conn.execute(
            "SELECT played_at, color, result, opening_name, eco, time_control "
            "FROM games ORDER BY (result = '*') ASC, played_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _mistake_profile(journal: Journal) -> dict:
    """Counts over INSTRUCTIVE mistakes only.

    Non-instructive ones were already filtered out at analysis time because
    they're noise below the threshold-in-force at capture time (see
    `journal.py`'s schema notes) — showing them here would reintroduce
    exactly the noise that filter exists to remove.
    """
    with journal.read() as conn:
        rows = conn.execute(
            "SELECT severity, phase, class FROM mistakes WHERE instructive = 1"
        ).fetchall()
    by_severity: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for r in rows:
        by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + 1
        by_phase[r["phase"]] = by_phase.get(r["phase"], 0) + 1
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
    return {"total": len(rows), "by_severity": by_severity, "by_phase": by_phase, "by_class": by_class}


def _drills_due(journal: Journal) -> int:
    """How many active drills are due right now — a live number, not a snapshot."""
    with journal.read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM drills WHERE retired = 0 AND due_at <= ?",
            (utc_now_iso(),),
        ).fetchone()
    return int(row["n"]) if row is not None else 0


def _live_game_facts(pool: Optional[EnginePool], lg: LiveGame) -> dict:
    """Facts about the in-progress or just-finished live game.

    ROADMAP.md §0 is the reason this function exists at all: **while the
    game is unfinished, `best_uci`, the PV, and the motifs baked into that
    best line must never reach the briefing** — those three are exactly
    the answer the metered hint ladder (`lg.hint_credits`) exists to sell,
    and leaking them into chat would let a player bypass it for free.
    Once `lg.terminated` is True the result is fixed and the restriction
    in ROADMAP.md's §0 explicitly lifts, so full disclosure resumes.
    """
    facts: dict = {
        "fen": lg.board.fen(),
        "side_to_move": "white" if lg.board.turn else "black",
        "moves_san": " ".join(lg.pgn_so_far),
        "move_number": lg.board.fullmove_number,
        "hint_credits": lg.hint_credits,
        "user_color": "white" if lg.user_color else "black",
        "engine_elo": lg.engine_elo,
        "white_ms": lg.white_ms,
        "black_ms": lg.black_ms,
        "terminated": lg.terminated,
        "result": lg.result,
    }
    if pool is None:
        return facts
    try:
        lines = pool.analyst_analyse(lg.board.fen(), depth=DEFAULT_DEPTH, multipv=1)
    except Exception:
        # An engine hiccup degrades the briefing to the facts above rather
        # than raising into the SSE stream — the coach can still talk
        # about history, process and the clock with zero live eval.
        return facts
    if not lines:
        return facts
    line = lines[0]
    facts["eval_cp"] = line.cp
    facts["eval_mate"] = line.mate
    if not lg.terminated:
        return facts
    facts["pv"] = line.pv
    facts["best_uci"] = line.best_uci
    try:
        best_move = chess.Move.from_uci(line.best_uci)
        facts["motifs"] = detect_motifs_before_move(lg.board.copy(), best_move, line.best_uci)
    except (ValueError, chess.InvalidMoveError):
        pass
    return facts


_ROUTE_LABELS = {
    "play": ("la vue partie en cours (plateau de jeu)", "the play view (live board)"),
    "review": ("la vue analyse d'une partie (review)", "the review view"),
    "drills": ("la vue exercices (drills)", "the drills view"),
    "openings": ("la vue répertoire d'ouvertures", "the openings view"),
    "study": ("la vue étude", "the study view"),
}


def _route_label(route: Optional[str], fr: bool) -> str:
    if route is None:
        return "inconnue" if fr else "unknown"
    label = _ROUTE_LABELS.get(route)
    if label is None:
        return route  # an unmapped route name is still more useful than "unknown"
    return label[0] if fr else label[1]


def _result_label(result: str, fr: bool) -> str:
    table = {
        "win": ("victoire", "win"), "loss": ("défaite", "loss"),
        "draw": ("nulle", "draw"), "*": ("inachevée", "unfinished"),
    }
    fr_txt, en_txt = table.get(result, (result, result))
    return fr_txt if fr else en_txt


def _color_label(color: str, fr: bool) -> str:
    table = {"white": ("blancs", "white"), "black": ("noirs", "black")}
    fr_txt, en_txt = table.get(color, (color, color))
    return fr_txt if fr else en_txt


def _mmss(ms: Optional[int]) -> str:
    """A clock reading as m:ss, never raw milliseconds.

    Whatever shape a fact is given in, the model quotes it straight back at
    the student — and "845000ms" is both unreadable and an invitation to
    mis-convert. Seconds are floored, which is how a chess clock displays
    the time remaining anyway.
    """
    if ms is None:
        return "—"
    total = max(0, ms // 1000)
    return f"{total // 60}:{total % 60:02d}"


def _day(ts: Optional[str]) -> str:
    """An ISO timestamp trimmed to its calendar day.

    `played_at` is stored full-precision ("2026.09.19T18:31:28+00:00"), and
    a model handed that will echo it verbatim into prose. The day is the
    only part a coach ever says out loud.
    """
    if not ts:
        return "—"
    return ts.replace(".", "-", 2).split("T")[0]


def _counts_str(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) if counts else "—"


def _build_briefing(
    journal: Journal,
    pool: Optional[EnginePool],
    lg: Optional[LiveGame],
    lang: str,
    route: Optional[str],
) -> str:
    """The full student dossier for one chat turn, as flat labelled lines.

    This is machine context the model reads, not prose it composes — hence
    short declarative lines rather than paragraphs. It is built fresh on
    EVERY turn (never cached) so a live game's clock, FEN and hint credits
    are always current, and it must produce something coherent even on a
    brand-new, completely empty journal: that's exactly the state a
    first-time user's first message arrives in, and the old behaviour of
    saying almost nothing outside a live game is the bug this replaces.
    """
    fr = lang == "fr"
    record = _record_summary(journal)
    recent = _recent_games(journal)
    profile = _mistake_profile(journal)
    recurring = _top_recurring_motifs(journal)
    due = _drills_due(journal)

    lines: list[str] = []

    if fr:
        lines.append(
            "Tu es le coach d'échecs intégré à \"Chess Coach\", une application "
            "d'entraînement que l'élève fait tourner lui-même, en local."
        )
        lines.append(
            "La personne dans cette conversation EST l'élève. Tu es à l'intérieur "
            "de l'app et tu vois son journal d'entraînement (parties, erreurs, "
            "exercices). Tu n'as accès à RIEN d'autre : pas de Google Calendar, "
            "pas d'iMessage, pas de compte Chess.com ou Lichess, rien en dehors de "
            "cette app. Ne propose jamais ce genre de choses."
        )
        lines.append(f"Vue actuelle de l'élève : {_route_label(route, fr)}.")
        lines.append(
            f"Palmarès : {record['total']} parties enregistrées — "
            f"{record['wins']} victoires, {record['losses']} défaites, "
            f"{record['draws']} nulles, {record['unfinished']} inachevées/abandonnées "
            f"(à ne pas compter comme des défaites). Dernière partie : "
            f"{_day(record['last_played_at']) if record['last_played_at'] else 'jamais'}."
        )
    else:
        lines.append(
            "You are the chess coach built into \"Chess Coach\", a training app "
            "the student runs themselves, locally."
        )
        lines.append(
            "The person in this conversation IS the student. You are inside the "
            "app and can see their training journal (games, mistakes, drills). "
            "You have access to NOTHING else: no Google Calendar, no iMessage, "
            "no Chess.com or Lichess account, nothing outside this app. Never "
            "offer those."
        )
        lines.append(f"Student's current view: {_route_label(route, fr)}.")
        lines.append(
            f"Record: {record['total']} games logged — {record['wins']} wins, "
            f"{record['losses']} losses, {record['draws']} draws, "
            f"{record['unfinished']} unfinished/abandoned (do not count these as "
            f"losses). Last played: {_day(record['last_played_at']) if record['last_played_at'] else 'never'}."
        )

    if recent:
        lines.append("Parties récentes :" if fr else "Recent games:")
        for g in recent:
            # PGN headers write a literal "?" for a tag the source didn't
            # fill in, so an empty opening arrives here as a truthy string
            # rather than as NULL — both spellings mean "not identified".
            raw_opening = (g["opening_name"] or "").strip()
            opening = raw_opening if raw_opening not in ("", "?") else (
                "ouverture non identifiée" if fr else "opening not identified"
            )
            raw_eco = (g["eco"] or "").strip()
            eco = f" ({raw_eco})" if raw_eco not in ("", "?") else ""
            lines.append(
                f"  - {_day(g['played_at'])} · {_color_label(g['color'], fr)} · "
                f"{_result_label(g['result'], fr)} · {opening}{eco}"
            )
    else:
        lines.append("Aucune partie enregistrée pour l'instant." if fr else "No games recorded yet.")

    if profile["total"] == 0:
        lines.append(
            "Aucune erreur instructive enregistrée pour l'instant." if fr
            else "No instructive mistakes recorded yet."
        )
    else:
        lines.append(
            (
                f"Profil d'erreurs instructives : {profile['total']} au total — "
                f"sévérité : {_counts_str(profile['by_severity'])} ; "
                f"phase : {_counts_str(profile['by_phase'])} ; "
                f"type : {_counts_str(profile['by_class'])}."
            ) if fr else (
                f"Instructive mistake profile: {profile['total']} total — "
                f"severity: {_counts_str(profile['by_severity'])}; "
                f"phase: {_counts_str(profile['by_phase'])}; "
                f"class: {_counts_str(profile['by_class'])}."
            )
        )
    if recurring:
        motif_txt = ", ".join(f"{m} ({n}x)" for m, n in recurring)
        lines.append(
            f"Motifs récurrents à travailler : {motif_txt}." if fr
            else f"Recurring weak motifs: {motif_txt}."
        )
    lines.append(f"Exercices (drills) dus maintenant : {due}." if fr else f"Drills due right now: {due}.")

    if lg is not None:
        facts = _live_game_facts(pool, lg)
        lines.append("")
        if not lg.terminated:
            lines.append(
                "Partie en cours (NON terminée) :" if fr else "Live game (NOT finished):"
            )
            lines.append(f"FEN : {facts['fen']}" if fr else f"FEN: {facts['fen']}")
            lines.append(
                f"Trait aux {_color_label(facts['side_to_move'], fr)}." if fr
                else f"Side to move: {facts['side_to_move']}."
            )
            lines.append(
                f"Coups joués : {facts['moves_san'] or '(aucun coup encore)'} "
                f"(coup n°{facts['move_number']})." if fr
                else f"Moves so far: {facts['moves_san'] or '(no moves yet)'} "
                     f"(move #{facts['move_number']})."
            )
            lines.append(
                f"L'élève joue les {_color_label(facts['user_color'], fr)} "
                f"contre le moteur (Elo {facts['engine_elo']})." if fr
                else f"Student is playing {facts['user_color']} against the "
                     f"engine (Elo {facts['engine_elo']})."
            )
            clock = f"blancs {_mmss(facts['white_ms'])}, noirs {_mmss(facts['black_ms'])}" if facts["white_ms"] is not None else "illimitée"
            clock_en = f"white {_mmss(facts['white_ms'])}, black {_mmss(facts['black_ms'])}" if facts["white_ms"] is not None else "unlimited"
            lines.append(f"Pendule : {clock}." if fr else f"Clock: {clock_en}.")
            if "eval_cp" in facts:
                eval_txt = f"mat en {facts['eval_mate']}" if facts.get("eval_mate") else f"{facts['eval_cp']} centipawns"
                eval_txt_en = f"mate in {facts['eval_mate']}" if facts.get("eval_mate") else f"{facts['eval_cp']} centipawns"
                lines.append(f"Évaluation moteur (trait courant) : {eval_txt}." if fr else f"Engine evaluation (side to move): {eval_txt_en}.")
            lines.append(
                f"Crédits d'indice restants : {facts['hint_credits']}." if fr
                else f"Hint credits remaining: {facts['hint_credits']}."
            )
            lines.append(
                "RAPPEL : cette partie n'est PAS terminée — tu ne dois nommer aucun "
                "coup, pièce ou case avant que l'élève ait dépensé un indice." if fr
                else "REMINDER: this game is NOT over — you must not name any move, "
                     "piece or square before the student spends a hint."
            )
        else:
            lines.append(
                f"Dernière partie jouée (TERMINÉE, résultat : {_result_label(facts['result'] or '*', fr)}) :" if fr
                else f"Last game played (FINISHED, result: {_result_label(facts['result'] or '*', fr)}):"
            )
            lines.append(f"FEN finale : {facts['fen']}" if fr else f"Final FEN: {facts['fen']}")
            lines.append(
                f"Coups joués : {facts['moves_san'] or '(aucun coup)'}." if fr
                else f"Moves played: {facts['moves_san'] or '(no moves)'}."
            )
            if "eval_cp" in facts:
                eval_txt = f"mat en {facts['eval_mate']}" if facts.get("eval_mate") else f"{facts['eval_cp']} centipawns"
                eval_txt_en = f"mate in {facts['eval_mate']}" if facts.get("eval_mate") else f"{facts['eval_cp']} centipawns"
                lines.append(f"Évaluation moteur (position finale) : {eval_txt}." if fr else f"Engine evaluation (final position): {eval_txt_en}.")
            if facts.get("best_uci"):
                lines.append(f"Meilleur coup du moteur : {facts['best_uci']}." if fr else f"Engine's best move: {facts['best_uci']}.")
            if facts.get("pv"):
                lines.append(f"Ligne principale : {' '.join(facts['pv'])}." if fr else f"Engine's best line: {' '.join(facts['pv'])}.")
            if facts.get("motifs"):
                lines.append(
                    f"Motifs détectés dans la meilleure ligne : {', '.join(facts['motifs'])}." if fr
                    else f"Detected motifs in the best line: {', '.join(facts['motifs'])}."
                )
            lines.append(
                "La partie est terminée : tu peux maintenant nommer des coups, "
                "des pièces et des cases librement." if fr
                else "The game is over: you may now name moves, pieces and "
                     "squares freely."
            )

    lines.append("")
    if fr:
        lines.append("Règles pour ta réponse :")
        lines.append(
            "- Réponds en français, sur un ton chaleureux, direct et personnel. "
            "Paragraphes courts. Parle en coach qui connaît son élève, en tenant "
            "compte de son historique ci-dessus."
        )
        lines.append(
            "- Les faits moteur donnés ci-dessus sont la SEULE source de vérité "
            "sur l'évaluation et les meilleurs coups. Ne les remets jamais en "
            "question, n'invente jamais un coup, une position, un nom d'ouverture "
            "ou une statistique absente d'ici. Si une information n'est pas dans "
            "ce contexte, dis clairement qu'elle n'est pas encore enregistrée."
        )
        lines.append("- Ta réponse est du texte affiché à l'élève ; elle n'est jamais interprétée comme un coup à jouer.")
        lines.append(
            "- Tant qu'une partie en cours n'est PAS terminée, ne nomme jamais un "
            "coup, une pièce ou une case : pose des questions, oriente sur le "
            "PROCESSUS. Si l'élève veut un coup concret, renvoie-le vers le "
            "bouton Indice (qui coûte un crédit) et précise combien il lui en "
            "reste. Une fois la partie terminée, cette restriction disparaît "
            "complètement."
        )
        lines.append("- Ne prétends jamais pouvoir faire quoi que ce soit en dehors de cette application.")
    else:
        lines.append("Rules for your reply:")
        lines.append(
            "- Reply in English, warm, direct and personal. Short paragraphs. "
            "Talk like a coach who knows this student, using their history above."
        )
        lines.append(
            "- The engine facts above are the SOLE source of truth about "
            "evaluation and best moves. Never second-guess them, never invent a "
            "move, a position, an opening name or a statistic not given above. "
            "If something is not in this context, say plainly it isn't recorded yet."
        )
        lines.append("- Your reply is prose displayed to the student; it is never parsed as a move or executed.")
        lines.append(
            "- While a live game is NOT finished, never name a move, a piece or "
            "a square: ask questions and point at PROCESS instead. If the "
            "student wants a concrete move, point them to the Hint button "
            "(costs a credit) and say how many they have left. Once the game "
            "is over, this restriction lifts completely."
        )
        lines.append("- Never claim you can do anything outside this app.")

    return "\n".join(lines)


def _compose_messages(briefing: str, messages: list[ChatMessage], lang: str) -> list[dict]:
    """Re-state the briefing inside the LAST user turn, not just the `system` slot.

    Hermes is an agent runtime with its own system persona: a gateway that
    layers (or substitutes) its own system prompt will silently override
    or drop whatever we put in the `system` role — that's exactly how a
    student asking "Where is my parties?" got an answer about Google
    Calendar and iMessage instead of their journal. Every gateway, by
    contrast, has to preserve the words the user actually typed, so
    wrapping the briefing around the final user message is the one
    placement nothing downstream can discard. The `system` entry is kept
    too since it costs nothing and does real work on gateways that honour it.

    Builds and returns new dicts throughout; never mutates `messages` or
    the `ChatMessage` objects inside it.
    """
    fr = lang == "fr"
    header = (
        "[CONTEXTE APPLICATION — injecté automatiquement, ce n'est pas un message de l'élève]"
        if fr else
        "[APPLICATION CONTEXT — injected automatically, this is not something the student typed]"
    )
    footer = "[FIN DU CONTEXTE]" if fr else "[END OF CONTEXT]"

    base = [m.model_dump() for m in messages]
    composed: list[dict] = [{"role": "system", "content": briefing}]

    last_user_idx = next(
        (i for i in range(len(base) - 1, -1, -1) if base[i]["role"] == "user"), None
    )
    if last_user_idx is None:
        # Shouldn't happen — the composer always sends the student's own
        # message last — but a conversation with no user turn at all must
        # still carry the briefing somewhere a gateway can't drop it.
        composed.extend(base)
        composed.append({"role": "user", "content": f"{header}\n{briefing}\n{footer}"})
        return composed

    for i, m in enumerate(base):
        if i == last_user_idx:
            composed.append({
                "role": m["role"],
                "content": f"{header}\n{briefing}\n{footer}\n\n{m['content']}",
            })
        else:
            composed.append(dict(m))
    return composed


def build_router(journal: Journal, pool: EnginePool, live_games: dict[str, LiveGame]) -> APIRouter:
    r = APIRouter()

    @r.get("/coach/status", response_model=CoachStatus)
    async def coach_status() -> CoachStatus:
        available, reason = await _check_availability()
        return CoachStatus(available=available, reason=reason, base_url=_base_url())

    @r.post("/coach/chat")
    async def coach_chat(req: ChatRequest) -> StreamingResponse:
        fr = req.lang == "fr"

        async def gen() -> AsyncIterator[str]:
            available, reason = await _check_availability()
            if not available:
                offline_txt = (
                    f"Le coach est hors ligne pour l'instant ({reason}). "
                    "L'analyse locale, les indices et la review fonctionnent normalement."
                    if fr else
                    f"The coach is offline right now ({reason}). Local analysis, hints, and review are unaffected."
                )
                yield _sse({"delta": offline_txt})
                yield "data: [DONE]\n\n"
                return

            lg = live_games.get(req.game_id) if req.game_id else None
            briefing = _build_briefing(journal, pool, lg, req.lang, req.route)
            payload = {
                "model": os.environ.get("HERMES_MODEL", "hermes"),
                "stream": True,
                "messages": _compose_messages(briefing, req.messages, req.lang),
            }
            headers = {"Content-Type": "application/json"}
            token = _load_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"

            try:
                async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                    async with client.stream(
                        "POST", f"{_base_url()}/chat/completions", json=payload, headers=headers,
                    ) as resp:
                        if resp.status_code >= 400:
                            error_txt = (
                                f"La passerelle du coach a renvoyé une erreur (HTTP {resp.status_code})."
                                if fr else
                                f"The coach gateway returned an error (HTTP {resp.status_code})."
                            )
                            yield _sse({"delta": error_txt})
                            yield "data: [DONE]\n\n"
                            return
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                                delta = obj["choices"][0]["delta"].get("content")
                            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                                continue
                            if delta:
                                yield _sse({"delta": delta})
            except httpx.HTTPError:
                unreachable_txt = (
                    "Impossible de joindre la passerelle du coach. L'analyse locale fonctionne toujours."
                    if fr else
                    "Could not reach the coach gateway. Local analysis still works."
                )
                yield _sse({"delta": unreachable_txt})
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return r


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def generate_review_summary(
    *,
    accuracy: float,
    acpl: Optional[int],
    n_mistakes: int,
    n_highlights: int,
    worst_severity: Optional[str],
) -> tuple[str, str]:
    """Reusable helper for `api_review.py`'s `/summary` endpoint.

    Tries Hermes for a richer summary; falls back to the deterministic
    local template on any unavailability or failure. This is the ONLY
    function outside this module's own router that other modules should
    call to get coach-generated text — it still makes the sole HTTP call
    to Hermes itself, so the "only api_coach.py talks to an LLM" rule
    holds even though `api_review.py` triggers it.

    Returns (text, source) where source is "hermes" or "local" so the
    caller can be transparent with the client about where the text
    came from.
    """
    fallback = local_review_summary(
        accuracy=accuracy, acpl=acpl, n_mistakes=n_mistakes,
        n_highlights=n_highlights, worst_severity=worst_severity,
    )
    available, _ = await _check_availability()
    if not available:
        return fallback, "local"

    token = _load_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    prompt = (
        "Write a short (2-4 sentence), encouraging but honest summary of this "
        f"chess game for the player. Accuracy: {accuracy:.1f}%. "
        f"Average centipawn loss: {acpl if acpl is not None else 'n/a'}. "
        f"Instructive mistakes: {n_mistakes} (worst: {worst_severity or 'none'}). "
        f"Good highlighted moments: {n_highlights}. "
        "Do not invent specific moves or positions you were not given."
    )
    payload = {
        "model": os.environ.get("HERMES_MODEL", "hermes"),
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are a concise, honest chess coach."},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
            resp = await client.post(f"{_base_url()}/chat/completions", json=payload, headers=headers)
        if resp.status_code < 400:
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            if text:
                return text, "hermes"
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return fallback, "local"


__all__ = [
    "build_router", "CoachStatus", "ChatMessage", "ChatRequest",
    "generate_review_summary",
]
