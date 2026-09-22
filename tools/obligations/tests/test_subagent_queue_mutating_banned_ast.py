#!/usr/bin/env python3
"""Regression tests for the AST-aware ``subagent_queue_mutating_banned``
predicate + the shared ``session-task queue`` subcommand detector.

Background (the bug this pins): a subagent was observed running
``session-task queue add`` (creating its OWN queue item) and then
``done`` / ``abandon`` on work the MAIN loop owns -- a protocol violation.
A Layer-2 obligation predicate (`subagent_queue_mutating_banned`) already
existed to DENY this, yet the incident still happened.

ROOT CAUSE: the predicate's subcommand detector was an ANCHORED regex
(``^\\s*(?:\\S*/)?session-task\\s+queue\\s+([a-z-]+)``) -- it only matched a
``session-task queue add`` at the very START of the command string. Every
COMPOUND / WRAPPED / SUBSTITUTED shape slipped past it (returning "not a
queue command" -> ALLOW):

    cd /x && session-task queue add ...     # after a `&&`
    env FOO=1 session-task queue done ...    # behind an env-assignment
    true; session-task queue abandon ...     # after a `;`
    OUT=$(session-task queue add ...)         # inside a command sub
    bash -c "session-task queue add ..."      # inside a -c body

The sibling ``no_backgrounded_watcher`` / privilege-escalation gates in the
SAME file were already AST-aware (via ``shell_ast``); the queue detector was
not. The fix routes the detector through ``shell_ast.subcommands_after`` so a
banned subcommand in ANY command position is caught, while preserving:

  * read-only subcommands (list/show/scope/...) -> ALLOW
  * ``register`` (the sanctioned rotated-q-id recovery) -> ALLOW
  * a mention inside quoted-arg / heredoc DATA -> ALLOW (not a real command)
  * main-loop callers (no agent_id) -> ALLOW (scope guard)

Loads the ``obligations`` CLI (no .py suffix) as a module via importlib, same
pattern as ``test_is_subagent_context.py``.

Run::

    uv run --python 3.11 --with pytest \\
        pytest tools/obligations/tests/test_subagent_queue_mutating_banned_ast.py -v
"""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
OBLIGATIONS = HERE.parent / "obligations"


def _load_obligations():
    spec = importlib.util.spec_from_loader(
        "obligations_cli",
        importlib.machinery.SourceFileLoader(
            "obligations_cli", str(OBLIGATIONS)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


obl = _load_obligations()
ev = obl._eval_predicate
subcmds = obl._session_task_queue_subcommands
banned_sub = obl._session_task_queue_banned_subcommand

PRED = {"kind": "subagent_queue_mutating_banned", "params": {}}
# Wrapped exactly as obligations-init seeds it in production.
WRAPPED = {
    "kind": "all_of",
    "params": {
        "predicates": [
            {"kind": "is_main_loop", "params": {"negate": True}},
            {"kind": "subagent_queue_mutating_banned", "params": {}},
        ]
    },
}

# A representative subagent hook context and a main-loop one.
SUBAGENT = dict(agent_id="agent-abc123", agent_type="general-purpose")
MAINLOOP = dict(agent_id=None, agent_type="repl_main_thread")


def _allow(pred, cmd, **ctx):
    ok, _why = ev(pred, "Bash", cmd, **ctx)
    return ok


# ---------------------------------------------------------------------------
# The shared detector (_session_task_queue_subcommands) -- the AST core.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,expected", [
    # Leading position (the ONLY shape the old anchored regex caught).
    ("session-task queue add x", {"add"}),
    ("/usr/local/bin/session-task queue add x", {"add"}),
    ("session-task   queue   done q-1", {"done"}),
    # Compound / wrapped / substituted -- the shapes the regex MISSED.
    ("cd /tmp && session-task queue add x", {"add"}),
    ("env FOO=1 session-task queue done q-1", {"done"}),
    ("true; session-task queue abandon q-1 --reason y", {"abandon"}),
    ("OUT=$(session-task queue add x)", {"add"}),
    ('bash -c "session-task queue done q-1"', {"done"}),
    ("sudo session-task queue block q-1 --reason z", {"block"}),
    # Multiple invocations in one command.
    ("session-task queue add a && session-task queue register q-1",
     {"add", "register"}),
    # Read-only + recovery still detected (as their own tokens).
    ("session-task queue register q-1", {"register"}),
    ("session-task queue list", {"list"}),
    # NOT a real command: quoted-arg / echo DATA.
    ("echo session-task queue add", set()),
    ("session-task queue add 'must not run session-task queue done'", {"add"}),
    # Not a session-task command at all.
    ("ls -la", set()),
    ("", set()),
])
def test_detector_catches_all_positions(cmd, expected):
    assert subcmds(cmd) == expected


# ---------------------------------------------------------------------------
# The banned-subcommand helper.
# ---------------------------------------------------------------------------

def test_banned_helper_picks_mutating():
    assert banned_sub("cd /x && session-task queue add y") == "add"
    assert banned_sub("session-task queue register q-1") is None
    assert banned_sub("session-task queue list") is None
    assert banned_sub("ls") is None
    # When both a banned and an allowed subcommand appear, a banned one wins.
    assert banned_sub(
        "session-task queue register q-1 && session-task queue add y") == "add"


# ---------------------------------------------------------------------------
# The predicate: subagent MUTATION shapes must DENY.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "session-task queue add \"x\" --scope repo:foo",
    "cd /tmp && session-task queue add x",              # the core bypass
    "env FOO=1 session-task queue add x",
    "true; session-task queue add x",
    "OUT=$(session-task queue add x)",
    'bash -c "session-task queue add x"',
    "session-task queue done q-1",
    "session-task queue abandon q-1 --reason done",
    "cd /w && session-task queue done q-1",
    "session-task queue block q-1 --reason x",
    "session-task queue unblock q-1",
    "session-task queue wedge q-1",
    "session-task queue unwedge q-1",
    "session-task queue force-start q-1",
    "session-task queue promote q-1",
    "session-task queue prune",
])
def test_subagent_mutation_denied(cmd):
    # Bare predicate (scope guard applied separately).
    assert _allow(PRED, cmd, **SUBAGENT) is False
    # And through the production all_of wrapper.
    assert _allow(WRAPPED, cmd, **SUBAGENT) is False


# ---------------------------------------------------------------------------
# The predicate: legit subagent paths must still ALLOW.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "session-task queue register q-1",                 # rotated-q-id recovery
    "cd /x && session-task queue register q-9",
    "session-task queue list",
    "session-task queue show q-1",
    "session-task queue spawn-check q-1",
    "session-task queue ready",
    "session-task queue status",
    "echo session-task queue add",                     # DATA, not a command
    "ls -la",                                          # unrelated command
])
def test_subagent_allowed_paths(cmd):
    assert _allow(PRED, cmd, **SUBAGENT) is True
    assert _allow(WRAPPED, cmd, **SUBAGENT) is True


def test_register_is_allowed_recovery():
    """register is the sanctioned rotated-q-id recovery -- NEVER banned.

    (It cannot create an item: the session-task CLI refuses a register of a
    q-id the queue has no record of, so it can't be a back-door to add.)"""
    assert _allow(PRED, "session-task queue register q-NEW", **SUBAGENT) is True


# ---------------------------------------------------------------------------
# Main-loop callers must always be allowed (the all_of scope guard).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "session-task queue add x",
    "cd /x && session-task queue add y",
    "session-task queue done q-1",
    "session-task queue abandon q-1 --reason x",
])
def test_main_loop_never_gated(cmd):
    # The production wrapper short-circuits to ALLOW for the main loop
    # (is_main_loop {negate: true} FAILS -> all_of treats it as inactive).
    assert _allow(WRAPPED, cmd, **MAINLOOP) is True


# ---------------------------------------------------------------------------
# Non-Bash tools + empty commands -> default-open ALLOW.
# ---------------------------------------------------------------------------

def test_non_bash_tool_allowed():
    ok, _ = ev(PRED, "Read", "session-task queue add x", **SUBAGENT)
    assert ok is True


def test_empty_command_allowed():
    assert _allow(PRED, "   ", **SUBAGENT) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
