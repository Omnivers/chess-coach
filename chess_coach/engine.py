"""Phase 1 — Stockfish UCI subprocess wrapper.

The truth layer for Phase 1. Speaks UCI to a Stockfish process; nothing
else in the codebase talks to an engine directly.

Why a subprocess and not python-chess's `SimpleEngine`:
- We want explicit control over Threads/Hash/MultiPV on a per-call basis.
- We want the ability to run a *batch* of positions against a *single*
  engine session, which is much faster than spawning one process per
  position. python-chess's wrapper supports this too, but the protocol
  surface here is small enough that we own it.

UCI is a line-based protocol. The subprocess is started in `__init__`,
`quit` is sent on close. We do not use `python-chess`'s `EngineProtocol`
because we want to be able to swap engines (lc0, etc.) without changing
the call site — only the binary path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Iterable, Optional


# UCI option keys we care about. Anything not listed here is left at the
# engine's default.
DEFAULT_THREADS = 1
DEFAULT_HASH_MB = 64
DEFAULT_MULTIPV = 3
DEFAULT_DEPTH = 18


@dataclass(frozen=True)
class EngineLine:
    """One multipv line from `analyse`."""
    multipv_rank: int
    depth: int
    cp: Optional[int]      # centipawns from the side-to-move POV; None if mate
    mate: Optional[int]    # mate-in-N; positive = side-to-move mates
    best_uci: str          # first move of the PV
    pv: list[str]          # full PV as UCI moves


@dataclass(frozen=True)
class EngineOptions:
    threads: int = DEFAULT_THREADS
    hash_mb: int = DEFAULT_HASH_MB
    multipv: int = DEFAULT_MULTIPV


class EngineError(RuntimeError):
    """Raised when the engine process dies or returns unexpected output."""


class StockfishEngine:
    """UCI subprocess wrapper.

    Thread-safety: a single instance is NOT safe to share across threads.
    Use one engine per analysis worker. The protocol is request/response
    so concurrent users need separate processes, not locks.
    """

    def __init__(
        self,
        binary: str | os.PathLike[str] = "stockfish",
        options: Optional[EngineOptions] = None,
        startup_timeout_s: float = 10.0,
    ) -> None:
        binary_path = Path(binary)
        # Allow `binary` to be a bare name and let PATH resolve it, but if
        # the caller passed a path that doesn't exist, fail loudly rather
        # than spawning a shell-injected name.
        if not binary_path.is_absolute() and not shutil.which(str(binary_path)):
            raise EngineError(f"engine binary not found on PATH: {binary_path}")
        if binary_path.is_absolute() and not binary_path.exists():
            raise EngineError(f"engine binary not found: {binary_path}")

        self._options = options or EngineOptions()
        self._proc: subprocess.Popen[str] = subprocess.Popen(
            [str(binary_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,  # line-buffered; UCI is line-based
        )
        self._stdout_lock = threading.Lock()  # only used if we ever go multi-threaded
        try:
            self._send("uci")
            self._expect("uciok", timeout_s=startup_timeout_s)
            self._apply_options()
            self._send("isready")
            self._expect("readyok", timeout_s=startup_timeout_s)
        except Exception:
            self.close()
            raise

    # --- public API --------------------------------------------------

    def analyse(
        self,
        fen: str,
        *,
        depth: int = DEFAULT_DEPTH,
        multipv: Optional[int] = None,
    ) -> list[EngineLine]:
        """Analyse `fen` (a FEN string) at the given depth, returning multipv lines.

        The lines are sorted by multipv rank (1 = engine's first choice).
        Each line's `cp` is from the side-to-move's POV. `mate` and `cp`
        are mutually exclusive; mate lines have `cp=None` and vice versa.
        """
        # UCI's `position fen` doesn't take "go depth" alongside multipv
        # for the multipv count — multipv is a UCI option, not a go
        # parameter. Apply it now if the caller wants something different
        # from the engine-level default.
        target_multipv = multipv if multipv is not None else self._options.multipv
        if target_multipv != self._options.multipv:
            self._send(f"setoption name MultiPV value {target_multipv}")
            self._send("isready")
            self._expect("readyok")

        self._send(f"position fen {fen}")
        self._send(f"go depth {depth}")

        # Collect info lines until `bestmove` arrives.
        lines_by_rank: dict[int, dict] = {}
        depth_reached: dict[int, int] = {}
        while True:
            raw = self._read_line()
            if raw.startswith("info"):
                info = _parse_info_line(raw)
                if info is None:
                    continue
                rank = info.get("multipv", 1)
                lines_by_rank[rank] = info
                if "depth" in info:
                    depth_reached[rank] = info["depth"]
            elif raw.startswith("bestmove"):
                break
            else:
                # Some engines emit `option`, `info string`, etc. Ignore.
                continue

        # Reset MultiPV back to default if we changed it for this call.
        if target_multipv != self._options.multipv:
            self._send(f"setoption name MultiPV value {self._options.multipv}")
            self._send("isready")
            self._expect("readyok")

        # Build EngineLine list, sorted by rank.
        out: list[EngineLine] = []
        for rank in sorted(lines_by_rank.keys()):
            info = lines_by_rank[rank]
            pv = info.get("pv", [])
            if not pv:
                continue
            best_uci = pv[0]
            # UCI emits mate scores with `score mate N` where N is the
            # distance to mate. Positive N = side-to-move mates the
            # opponent in N. We keep that convention; `cp` is None.
            out.append(EngineLine(
                multipv_rank=rank,
                depth=info.get("depth", depth_reached.get(rank, depth)),
                cp=info.get("cp"),
                mate=info.get("mate"),
                best_uci=best_uci,
                pv=pv,
            ))
        return out

    def close(self) -> None:
        """Send `quit` and wait. Idempotent."""
        proc = getattr(self, "_proc", None)
        if proc is None or proc.poll() is not None:
            return
        try:
            self._send("quit")
            proc.wait(timeout=2.0)
        except Exception:
            # Force-kill if quit doesn't get us out cleanly.
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            self._proc = None  # type: ignore[assignment]

    def __enter__(self) -> "StockfishEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- internals ---------------------------------------------------

    def _apply_options(self) -> None:
        opts = self._options
        self._send(f"setoption name Threads value {opts.threads}")
        self._send(f"setoption name Hash value {opts.hash_mb}")
        self._send(f"setoption name MultiPV value {opts.multipv}")

    def _send(self, line: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise EngineError("engine not running")
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    def _read_line(self) -> str:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise EngineError("engine not running")
        # Blocking read. UCI is line-based and Stockfish flushes; a normal
        # `readline()` is sufficient and avoids the timeout machinery.
        return proc.stdout.readline().strip()

    def _expect(self, token: str, *, timeout_s: float = 5.0) -> None:
        """Read lines until one starts with `token` or we time out."""
        deadline = time.monotonic() + timeout_s
        while True:
            if time.monotonic() > deadline:
                raise EngineError(f"timeout waiting for {token!r}")
            line = self._read_line()
            if line.startswith(token):
                return


def _parse_info_line(raw: str) -> Optional[dict]:
    """Parse a UCI `info ...` line into a dict.

    Stockfish emits one `info` line per search update. Fields can appear
    in any order; we walk tokens left-to-right.

    Keys we capture: depth, multipv, score (cp/mate), pv (list of UCI).
    Other fields (nps, hashfull, tbhits, time, nodes, seldepth, string)
    are ignored. `cp` and `mate` are mutually exclusive; whichever one
    appears in the line is set, the other stays None.
    """
    if not raw.startswith("info"):
        return None
    tokens = raw.split()
    out: dict = {"cp": None, "mate": None, "pv": []}
    i = 1  # skip "info"
    while i < len(tokens):
        tok = tokens[i]
        if tok == "depth":
            i += 1
            out["depth"] = int(tokens[i]); i += 1
        elif tok == "multipv":
            i += 1
            out["multipv"] = int(tokens[i]); i += 1
        elif tok == "score":
            i += 1
            kind = tokens[i]
            i += 1
            value = int(tokens[i]); i += 1
            if kind == "cp":
                out["cp"] = value
                out["mate"] = None
            elif kind == "mate":
                out["mate"] = value
                out["cp"] = None
        elif tok == "pv":
            i += 1
            pv = []
            while i < len(tokens) and _looks_like_uci(tokens[i]):
                pv.append(tokens[i]); i += 1
            out["pv"] = pv
        else:
            # Consume one token of value if the next looks like a value
            # for a known key we don't track. Cheap heuristic: skip the
            # value for `time`, `nodes`, `nps`, `hashfull`, `tbhits`,
            # `seldepth`, `string`, `currmove`, `currmovenumber`.
            if tok in {"time", "nodes", "nps", "hashfull", "tbhits",
                       "seldepth", "currmove", "currmovenumber"}:
                i += 1
                # Skip one value token.
                if i < len(tokens):
                    i += 1
            elif tok == "string":
                # Rest of the line is a free-form string. We're done.
                return out
            else:
                # Unknown token — skip one to avoid getting stuck.
                i += 1
    return out


def _looks_like_uci(token: str) -> bool:
    """Cheap UCI-move shape check: 4 or 5 lowercase letters, optional promotion."""
    if not token or len(token) < 4 or len(token) > 5:
        return False
    for ch in token[:4]:
        if not ("a" <= ch <= "h" or "1" <= ch <= "8"):
            return False
    if len(token) == 5 and token[4] not in "bnrq":
        return False
    return True


__all__ = [
    "StockfishEngine",
    "EngineLine",
    "EngineOptions",
    "EngineError",
    "DEFAULT_DEPTH",
    "DEFAULT_MULTIPV",
    "DEFAULT_THREADS",
    "DEFAULT_HASH_MB",
]
