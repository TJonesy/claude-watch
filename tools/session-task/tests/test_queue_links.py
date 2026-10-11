#!/usr/bin/env python3
"""Tests for structured queue-item links (`links` field).

Covers:
  * `queue add --chat/--issue/--link` stores links; `add --json`,
    `list --json` and `show` emit the normalized `links` object.
  * URL[=label] parsing, including `=` inside a query string.
  * http(s)-only validation (add / update / links all reject, exit 1, and
    leave the store untouched).
  * `queue links <id>`: show, --set-chat / --add-issue / --add-link /
    --remove, dedup + relabel, removing the last link drops the key.
  * `queue links` works on running / blocked / done items and on an item
    with a LIVE agent bound (pure metadata edit, like set-summary), and
    fires no pingme.
  * `queue update --chat/--issue/--link`: links-only update skips the
    live-agent guard; spec + links together still honours it; done items
    are refused as before.
  * Back-compat: a legacy store record with no `links` key (and one with a
    malformed `links` value) reads as the empty shape everywhere.
  * `resurrect` is covered indirectly: it copies `_item_links(old)`.

Run:
    uv run --python 3.11 --with pytest \\
        pytest tests/test_queue_links.py -v
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SESSION_TASK = Path(__file__).resolve().parent.parent / "session-task"

EMPTY = {"chat": None, "issues": [], "related": []}
CHAT = "https://chat.example/#/room/!abc:example.org"
ISSUE = "https://git.example/org/foo/issues/12"
PR = "https://git.example/org/foo/pulls/34"


def _env_for_tmp(tmp):
    env = dict(os.environ)
    env["HOME"] = str(tmp)
    env["PINGME_SESSION_TASK"] = "0"
    env["CLAUDE_AGENTS_STATE"] = str(Path(tmp, "active-agents.json"))
    env["CLAUDE_AGENTS_STATE_FALLBACK_BIN"] = ""
    Path(tmp, ".config/session").mkdir(parents=True, exist_ok=True)
    return env


def _run(env, *argv, timeout=15):
    cmd = [sys.executable, str(SESSION_TASK)] + list(argv)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=timeout)


def _add(env, desc, *extra, scope="resource:links-test"):
    r = _run(env, "queue", "add", desc, "--scope", scope, "--summary", "s",
             "--json", *extra)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _show(env, qid):
    r = _run(env, "queue", "show", qid)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _queue_path(tmp):
    return Path(tmp, ".config/session/queue.json")


def _raw_item(tmp, qid):
    data = json.loads(_queue_path(tmp).read_text())
    return next(it for it in data["items"] if it["id"] == qid)


def _links(env, qid, *argv):
    r = _run(env, "queue", "links", qid, *argv, "--json")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _write_live_agent_state(env, qid, agent_id="agentxyz"):
    state = {"agents": [{"queue_id": qid, "agent_id": agent_id,
                         "alive": True, "jsonl_age_seconds": 1}]}
    Path(env["CLAUDE_AGENTS_STATE"]).write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# queue add
# ---------------------------------------------------------------------------


def test_add_stores_links_and_emits_them():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d", "--chat", CHAT, "--issue", f"{ISSUE}=flaky deploy",
                 "--link", PR, "--link", "https://ci.example/o/r/actions/runs/9")
        want = {
            "chat": CHAT,
            "issues": [{"url": ISSUE, "label": "flaky deploy"}],
            "related": [
                {"url": PR, "label": None, "kind": "pr"},
                {"url": "https://ci.example/o/r/actions/runs/9",
                 "label": None, "kind": "ci"},
            ],
        }
        assert a["links"] == want
        assert _raw_item(tmp, a["id"])["links"] == want
        assert _show(env, a["id"])["links"] == want
        listed = json.loads(_run(env, "queue", "list", "--json").stdout)
        assert listed[0]["links"] == want


def test_add_without_links_keeps_legacy_store_shape():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d")
        assert a["links"] == EMPTY
        assert "links" not in _raw_item(tmp, a["id"])
        assert _show(env, a["id"])["links"] == EMPTY


def test_label_parsing_respects_query_strings():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(
            env, "d",
            "--link", "https://grafana.example/d/x?orgId=1&var=a",
            "--link", "https://grafana.example/d/y?orgId=2 queue board",
            "--link", "https://git.example/o/r/-/merge_requests/5=the MR",
            "--link", "https://terrakube.example/app/org/ws/7",
        )
        rel = a["links"]["related"]
        assert rel[0] == {"url": "https://grafana.example/d/x?orgId=1&var=a",
                          "label": None, "kind": "dashboard"}
        assert rel[1] == {"url": "https://grafana.example/d/y?orgId=2",
                          "label": "queue board", "kind": "dashboard"}
        assert rel[2] == {"url": "https://git.example/o/r/-/merge_requests/5",
                          "label": "the MR", "kind": "mr"}
        assert rel[3]["kind"] == "terrakube"


def test_non_http_urls_rejected_everywhere():
    bad = ["javascript:alert(1)", "ftp://x.example/f", "/relative/path",
           "https://", "data:text/html,hi", "JavaScript://x.example/%0a"]
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d")
        before = _queue_path(tmp).read_text()
        for url in bad:
            for argv in (
                ["queue", "add", "d2", "--scope", "resource:z", "--link", url],
                ["queue", "add", "d2", "--scope", "resource:z", "--chat", url],
                ["queue", "update", a["id"], "--issue", url],
                ["queue", "links", a["id"], "--set-chat", url],
                ["queue", "links", a["id"], "--add-link", url],
            ):
                r = _run(env, *argv)
                assert r.returncode == 1, (argv, r.stdout, r.stderr)
                assert "ERROR" in r.stderr
        # --chat takes no label, so embedded whitespace is an error too.
        r = _run(env, "queue", "links", a["id"], "--set-chat",
                 "https://x.example/a b")
        assert r.returncode == 1
        assert _queue_path(tmp).read_text() == before


# ---------------------------------------------------------------------------
# queue links
# ---------------------------------------------------------------------------


def test_links_show_edit_dedup_relabel_remove():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d")
        qid = a["id"]
        assert _links(env, qid) == {"id": qid, "changed": False, "links": EMPTY}

        out = _links(env, qid, "--set-chat", CHAT, "--add-issue", ISSUE,
                     "--add-link", PR)
        assert out["changed"] is True
        assert out["links"]["chat"] == CHAT
        assert "links_updated_at" in _raw_item(tmp, qid)

        # Re-adding is idempotent; a new label relabels in place.
        out = _links(env, qid, "--add-issue", ISSUE, "--add-link", PR)
        assert out["changed"] is False
        out = _links(env, qid, "--add-link", f"{PR}=fix PR")
        assert out["links"]["related"] == [
            {"url": PR, "label": "fix PR", "kind": "pr"}]

        # --remove drops from every slot; unknown URL only warns.
        r = _run(env, "queue", "links", qid, "--remove", CHAT,
                 "--remove", "https://nope.example/")
        assert r.returncode == 0, r.stderr
        assert "not linked" in r.stderr
        assert _show(env, qid)["links"]["chat"] is None

        _links(env, qid, "--remove", ISSUE, "--remove", PR)
        assert "links" not in _raw_item(tmp, qid)
        assert _show(env, qid)["links"] == EMPTY


def test_links_text_output():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d", "--chat", CHAT, "--link", f"{PR}=fix")
        r = _run(env, "queue", "links", a["id"])
        assert r.returncode == 0, r.stderr
        assert f"chat   : {CHAT}" in r.stdout
        assert f"link   : {PR}  [pr, fix]" in r.stdout
        r = _run(env, "queue", "list")
        assert f"chat   : {CHAT}" in r.stdout


def test_links_missing_id_exits_1():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        r = _run(env, "queue", "links", "q-nope", "--add-link", PR)
        assert r.returncode == 1
        assert "not found" in r.stderr


def test_links_editable_in_any_status_and_with_live_agent():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d", scope="resource:a")
        assert _run(env, "queue", "register", a["id"]).returncode == 0
        _write_live_agent_state(env, a["id"])
        assert _links(env, a["id"], "--add-link", PR)["changed"] is True

        r = _run(env, "queue", "block", a["id"], "--reason", "waiting")
        assert r.returncode == 0, r.stderr
        assert _links(env, a["id"], "--set-chat", CHAT)["changed"] is True

        b = _add(env, "d", scope="resource:b")
        assert _run(env, "queue", "register", b["id"]).returncode == 0
        assert _run(env, "queue", "done", b["id"]).returncode == 0
        out = _links(env, b["id"], "--add-link", PR)
        assert out["changed"] is True
        assert _show(env, b["id"])["status"] == "done"


def test_links_fires_no_pingme():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        bindir = Path(tmp, "bin")
        bindir.mkdir()
        log = Path(tmp, "pingme.log")
        shim = bindir / "pingme"
        shim.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
        shim.chmod(0o755)
        env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
        env.pop("PINGME_SESSION_TASK", None)
        a = _add(env, "d")
        log.unlink(missing_ok=True)
        _links(env, a["id"], "--set-chat", CHAT, "--add-link", PR)
        assert not log.exists()


# ---------------------------------------------------------------------------
# queue update
# ---------------------------------------------------------------------------


def test_update_links_only_skips_live_agent_guard():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d")
        assert _run(env, "queue", "register", a["id"]).returncode == 0
        _write_live_agent_state(env, a["id"])
        r = _run(env, "queue", "update", a["id"], "--chat", CHAT,
                  "--issue", ISSUE, "--json")
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert set(out["changes"]) == {"links"}
        assert out["changes"]["links"]["old"] == EMPTY
        assert out["item"]["links"]["chat"] == CHAT
        assert "spec_updated_at" not in out["item"]
        assert "links_updated_at" in out["item"]

        # Spec + links together still trips the guard, and writes nothing.
        r = _run(env, "queue", "update", a["id"], "--summary", "new",
                  "--link", PR)
        assert r.returncode == 2
        assert _show(env, a["id"])["links"]["related"] == []


def test_update_links_are_additive_and_chat_replaces():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d", "--chat", CHAT, "--link", PR)
        other_chat = "https://chat.example/#/room/!other:example.org"
        r = _run(env, "queue", "update", a["id"], "--chat", other_chat,
                  "--link", "https://ci.example/o/r/actions/runs/1",
                  "--summary", "edited")
        assert r.returncode == 0, r.stderr
        links = _show(env, a["id"])["links"]
        assert links["chat"] == other_chat
        assert [e["url"] for e in links["related"]] == [
            PR, "https://ci.example/o/r/actions/runs/1"]
        raw = _raw_item(tmp, a["id"])
        assert "spec_updated_at" in raw and "links_updated_at" in raw

        r = _run(env, "queue", "update", a["id"], "--link", PR)
        assert r.returncode == 0
        assert "no-op" in r.stdout


def test_update_links_refused_on_done_item():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "d")
        _run(env, "queue", "register", a["id"])
        _run(env, "queue", "done", a["id"])
        r = _run(env, "queue", "update", a["id"], "--link", PR)
        assert r.returncode == 1
        assert "update refused" in r.stderr


# ---------------------------------------------------------------------------
# Back-compat with records written before `links` existed
# ---------------------------------------------------------------------------


def test_legacy_and_malformed_records_read_as_empty():
    with tempfile.TemporaryDirectory() as tmp:
        env = _env_for_tmp(tmp)
        a = _add(env, "legacy")
        b = _add(env, "malformed", scope="resource:other")
        c = _add(env, "hand-edited", scope="resource:third")
        data = json.loads(_queue_path(tmp).read_text())
        for it in data["items"]:
            if it["id"] == b["id"]:
                it["links"] = "https://not-a-dict.example/"
            if it["id"] == c["id"]:
                it["links"] = {
                    "chat": "javascript:alert(1)",
                    "issues": [ISSUE, {"url": "ftp://x"}, 7, ISSUE],
                    "related": [{"url": PR, "kind": "  "}],
                }
        _queue_path(tmp).write_text(json.dumps(data))

        assert _show(env, a["id"])["links"] == EMPTY
        assert _show(env, b["id"])["links"] == EMPTY
        assert _show(env, c["id"])["links"] == {
            "chat": None,
            "issues": [{"url": ISSUE, "label": None}],
            "related": [{"url": PR, "label": None, "kind": "pr"}],
        }
        listed = {it["id"]: it for it in
                  json.loads(_run(env, "queue", "list", "--json").stdout)}
        assert listed[a["id"]]["links"] == EMPTY
        # Every other legacy field is passed through untouched.
        assert listed[a["id"]]["description"] == "legacy"

        # Editing a malformed record rewrites it in canonical shape.
        _links(env, b["id"], "--add-link", PR)
        assert _raw_item(tmp, b["id"])["links"]["related"][0]["url"] == PR


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
