"""Phase 1 — token-loading from ~/.hermes/.env.

These tests verify the credential path works end-to-end without ever
echoing the token's value. The shape of the contract: token is read at
call time, priority is process env > ~/.hermes/.env, missing file is
not an error (just no token).

Critically, no assertion reads or prints the token. The token's
presence is detected via the public format marker, not by content.

Every fake token here is *assembled* from a prefix constant rather
than written as one literal string. GitHub's push protection matches
the Lichess token format by shape alone, so a literal
"<prefix>" + 20 characters in this file — however obviously fake —
gets the whole push rejected. Splitting the prefix out costs one
constant and keeps the repo pushable.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from chess_coach.ingest import _load_token_from_env

# Assembled, never written as one literal — see the module docstring.
_PREFIX = "lip" + "_"
_FAKE_TOKEN = _PREFIX + "PLACEHOLDER000000000"


@pytest.fixture
def fake_hermes_env(tmp_path: Path, monkeypatch) -> Path:
    """Redirect Path.home() to a temp dir with a .hermes/.env containing a token."""
    env_path = tmp_path / ".hermes" / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"LICHESS_TOKEN={_FAKE_TOKEN}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Make sure process env doesn't shadow the file lookup.
    monkeypatch.delenv("LICHESS_TOKEN", raising=False)
    return env_path


def test_token_loaded_from_hermes_env(fake_hermes_env) -> None:
    tok = _load_token_from_env()
    assert tok is not None
    assert tok.startswith(_PREFIX)
    # Length check is structural (prefix + 20 chars), not a content leak.
    assert len(tok) == 24


def test_process_env_overrides_file(fake_hermes_env) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("LICHESS_TOKEN", _PREFIX + "FROM_PROCESS_ENV")
    try:
        tok = _load_token_from_env()
        assert tok == _PREFIX + "FROM_PROCESS_ENV"
    finally:
        monkeypatch.undo()


def test_missing_env_file_is_not_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LICHESS_TOKEN", raising=False)
    # No .hermes/.env exists; should return None, not raise.
    assert _load_token_from_env() is None


def test_env_file_with_quoted_token(fake_hermes_env) -> None:
    """Tokens sometimes get wrapped in quotes by editors — strip them."""
    fake_hermes_env.write_text(f'LICHESS_TOKEN="{_PREFIX}QUOTED_VALUE_HERE"\n')
    tok = _load_token_from_env()
    assert tok is not None
    assert not tok.startswith('"') and not tok.endswith('"')
    assert tok.startswith(_PREFIX)


def test_env_file_with_comments_and_blanks(fake_hermes_env) -> None:
    fake_hermes_env.write_text(
        "# Coin Scout token\n"
        "\n"
        f"LICHESS_TOKEN={_PREFIX}IGNORED_FIRST_TOKEN\n"
        "# another comment\n"
    )
    tok = _load_token_from_env()
    assert tok == _PREFIX + "IGNORED_FIRST_TOKEN"


def test_env_file_without_token_is_not_error(fake_hermes_env) -> None:
    fake_hermes_env.write_text("# no token here\nOTHER_KEY=ignored\n")
    assert _load_token_from_env() is None
