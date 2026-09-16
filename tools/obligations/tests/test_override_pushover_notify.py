#!/usr/bin/env python3
"""Tests for the hardened ``_pingme_override`` Pushover-notify path.

An audited ``obligations override`` disables EVERY safety gate, so its creation
MUST reach Andrew's phone. The historical implementation shelled to a ``pingme``
binary via ``shutil.which`` and silently no-op'd whenever pingme was absent from
the (subagent) PATH -- the common case -- and even when present used ``-p low``,
a priority Pushover often delivers silently. Both failure modes meant the most
consequential event in the system routinely produced NO phone alert.

The hardening keeps pingme as the preferred path (raised to a non-silent
priority) and adds a self-contained Pushover-API fallback that resolves creds
the same way ``session-task/queue-notify`` does. These tests pin:

  * the direct-POST fallback fires (to the ``OBLIGATIONS_PINGME_SINK`` seam) at
    a non-silent priority when pingme is absent from PATH;
  * pingme, when present, is invoked at ``-p high`` (never ``-p low``) and a
    successful pingme suppresses the fallback;
  * a failing / missing pingme falls THROUGH to the direct POST;
  * ``OBLIGATIONS_DISABLE_PINGME=1`` is a full no-op;
  * creds resolve from the queue-notify / pingme env files exactly as
    queue-notify resolves them (token + user), and a missing cred means the
    direct POST reports failure rather than raising.

Run::

    uv run --python 3.11 --with pytest \\
        pytest tools/obligations/tests/test_override_pushover_notify.py -v
"""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
OBLIGATIONS = HERE.parent / "obligations"


def _load_obligations():
    spec = importlib.util.spec_from_loader(
        "obligations_cli_pushover",
        importlib.machinery.SourceFileLoader(
            "obligations_cli_pushover", str(OBLIGATIONS)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


obl = _load_obligations()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every Pushover/pingme env var so a test starts from a known
    baseline and never reads the operator's real creds or sink."""
    for var in (
        "OBLIGATIONS_DISABLE_PINGME", "OBLIGATIONS_PINGME_SINK",
        "QUEUE_NOTIFY_TOKEN", "PUSHOVER_TOKEN", "PUSHOVER_USER",
        "QUEUE_NOTIFY_ENV", "PINGME_ENV",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _read_sink(sink: Path) -> list[dict]:
    return [json.loads(line) for line in
            sink.read_text().splitlines() if line.strip()]


# --- credential resolution (mirrors queue-notify) --------------------------

def test_resolve_token_prefers_env(monkeypatch):
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok-from-env")
    assert obl._pushover_resolve_token() == "tok-from-env"


def test_resolve_token_from_queue_notify_env_file(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.write_text('PUSHOVER_TOKEN="tok-from-file"\n')
    monkeypatch.setenv("QUEUE_NOTIFY_ENV", str(env))
    assert obl._pushover_resolve_token() == "tok-from-file"


def test_resolve_user_falls_back_to_pingme_env(tmp_path, monkeypatch):
    pingme_env = tmp_path / "pingme-env"
    pingme_env.write_text('export PUSHOVER_USER=usr-from-pingme\n')
    monkeypatch.setenv("PINGME_ENV", str(pingme_env))
    # No PUSHOVER_USER in env and no queue-notify override -> pingme file wins.
    monkeypatch.setenv("QUEUE_NOTIFY_ENV",
                       str(tmp_path / "no-such-queue-notify-env"))
    assert obl._pushover_resolve_user() == "usr-from-pingme"


def test_parse_env_file_tolerates_comments_and_quotes(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "# a comment\n\n"
        "export PUSHOVER_TOKEN='quoted-tok'\n"
        'PUSHOVER_USER = "spaced-user"\n'
        "garbage line without equals\n"
    )
    parsed = obl._pushover_parse_env_file(env)
    assert parsed["PUSHOVER_TOKEN"] == "quoted-tok"
    assert parsed["PUSHOVER_USER"] == "spaced-user"


# --- direct-POST fallback via the sink seam --------------------------------

def test_direct_post_writes_sink_at_high_priority(tmp_path, monkeypatch):
    sink = tmp_path / "sink.jsonl"
    monkeypatch.setenv("OBLIGATIONS_PINGME_SINK", str(sink))
    ok = obl._pushover_post("obligations override", "body text", priority=1)
    assert ok is True
    records = _read_sink(sink)
    assert len(records) == 1
    assert records[0]["priority"] == 1  # non-silent (high), never -1/low
    assert records[0]["message"] == "body text"
    assert records[0]["title"] == "obligations override"


def test_direct_post_missing_creds_returns_false(tmp_path, monkeypatch):
    # No sink, no token, no user -> reports failure, never raises. Point the
    # env-file lookups at nonexistent paths so a real host queue-notify/pingme
    # env file can't supply creds and turn this into a live network POST.
    monkeypatch.setenv("QUEUE_NOTIFY_ENV", str(tmp_path / "no-qn-env"))
    monkeypatch.setenv("PINGME_ENV", str(tmp_path / "no-pingme-env"))
    assert obl._pushover_post("t", "b", priority=1) is False


# --- _pingme_override end-to-end -------------------------------------------

def test_override_falls_back_to_direct_post_when_pingme_absent(
        tmp_path, monkeypatch):
    """pingme not on PATH -> the direct Pushover POST fires (to the sink) with
    the override id + reason + human duration, at a non-silent priority."""
    sink = tmp_path / "sink.jsonl"
    monkeypatch.setenv("OBLIGATIONS_PINGME_SINK", str(sink))
    monkeypatch.setattr(obl.shutil, "which", lambda _name: None)

    obl._pingme_override("ov-abc123", "debugging a wedge", 3600)

    records = _read_sink(sink)
    assert len(records) == 1
    rec = records[0]
    assert rec["priority"] == 1
    assert "ov-abc123" in rec["message"]
    assert "debugging a wedge" in rec["message"]
    assert "1.0h" in rec["message"]  # 3600s -> "1.0h" (human duration)
    assert rec["title"] == "obligations override"


def test_override_prefers_pingme_at_high_priority(tmp_path, monkeypatch):
    """pingme present + succeeds -> it is invoked at ``-p high`` (never low) and
    the direct-POST fallback does NOT fire."""
    sink = tmp_path / "sink.jsonl"
    monkeypatch.setenv("OBLIGATIONS_PINGME_SINK", str(sink))
    monkeypatch.setattr(obl.shutil, "which",
                        lambda name: "/usr/bin/pingme"
                        if name == "pingme" else None)

    calls = {}

    class _Proc:
        returncode = 0

    def _fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _Proc()

    monkeypatch.setattr(obl.subprocess, "run", _fake_run)

    obl._pingme_override("ov-xyz", "reason here", 90)

    assert calls["argv"][0] == "/usr/bin/pingme"
    assert "-p" in calls["argv"]
    prio = calls["argv"][calls["argv"].index("-p") + 1]
    assert prio == "high"  # raised from the old silent "low"
    assert prio != "low"
    # Successful pingme suppressed the fallback: nothing written to the sink.
    assert not sink.exists() or _read_sink(sink) == []


def test_override_falls_through_when_pingme_fails(tmp_path, monkeypatch):
    """pingme present but returns non-zero -> fall through to the direct POST."""
    sink = tmp_path / "sink.jsonl"
    monkeypatch.setenv("OBLIGATIONS_PINGME_SINK", str(sink))
    monkeypatch.setattr(obl.shutil, "which",
                        lambda name: "/usr/bin/pingme"
                        if name == "pingme" else None)

    class _Proc:
        returncode = 1

    monkeypatch.setattr(obl.subprocess, "run", lambda *a, **k: _Proc())

    obl._pingme_override("ov-fail", "boom", 30)

    records = _read_sink(sink)
    assert len(records) == 1
    assert "ov-fail" in records[0]["message"]
    assert records[0]["priority"] == 1


def test_override_disabled_is_full_noop(tmp_path, monkeypatch):
    """OBLIGATIONS_DISABLE_PINGME=1 -> neither pingme nor the direct POST run."""
    sink = tmp_path / "sink.jsonl"
    monkeypatch.setenv("OBLIGATIONS_PINGME_SINK", str(sink))
    monkeypatch.setenv("OBLIGATIONS_DISABLE_PINGME", "1")

    def _boom(*a, **k):
        raise AssertionError("pingme/which must not run when disabled")

    monkeypatch.setattr(obl.shutil, "which", _boom)
    monkeypatch.setattr(obl.subprocess, "run", _boom)

    obl._pingme_override("ov-none", "should not fire", 10)
    assert not sink.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
