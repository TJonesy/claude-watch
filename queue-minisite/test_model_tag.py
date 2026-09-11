#!/usr/bin/env python3
"""Tests for the per-row MODEL TAG on queue-minisite list entries.

Every list entry — running, pending, blocked, wedged, quarantined, done,
abandoned, and the unrecognised-status fallback — carries a short chip
naming WHICH MODEL ran the item ("opus" / "sonnet" / ...), with the raw
transcript id on hover. The chip lives in the card HEAD, the one part of
a row compact density never elides, so it is visible in compact mode too
(the operator's compact-mode screenshot showed queue id / priority /
token count / age / creator and no model anywhere).

Rules pinned here:

* the model is resolved by ``_shape`` from the same sources the /meta
  endpoint uses — a ``model`` stamped on the queue record, else the
  ARCHIVED transcript, else the owner's LIVE transcript;
* ``model_label`` is the family shorthand when the id is recognised and
  the RAW id otherwise — never an invented label;
* an item with no attributable model (workload / hostjob, pending,
  rotated-away transcript) gets ``""`` for both fields and renders NO
  chip — absent is absent, with no "unknown" placeholder;
* the 5s refresh.js renderer mirrors the Jinja macro (class parity), so
  the morphdom merge doesn't wipe the chip off the first paint;
* compact density keeps the chip (shrinks it; never hides it);
* the archive scan is memoised, so decorating every row doesn't re-read
  every transcript on every 5s tick.

Run::

    python3 queue-minisite/test_model_tag.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

SECTION_STATUSES = (
    "running",
    "pending",
    "blocked",
    "wedged",
    "quarantined",
    "done",
    "abandoned",
    "sideways",  # unrecognised -> the OTHER fallback section
)


def _item(item_id: str, status: str = "running", **over) -> dict:
    rec = {
        "id": item_id,
        "summary": f"summary {item_id}",
        "description": "",
        "scope": [],
        "status": status,
        "priority": 5,
        "created_by": "main-loop",
        "created_at": "2026-09-01T00:00:00+00:00",
        "registered_at": "2026-09-01T00:00:00+00:00",
        "completed_at": "2026-09-01T01:00:00+00:00",
        "abandoned_at": "2026-09-01T01:00:00+00:00",
        "blocked_at": "2026-09-01T01:00:00+00:00",
        "wedged_at": "2026-09-01T01:00:00+00:00",
        "quarantined_at": "2026-09-01T01:00:00+00:00",
    }
    rec.update(over)
    return rec


class ModelTagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="qmin-model-tag-")
        cls.queue_actual = Path(cls.tmp) / ".config/session/queue.json"
        cls.archive_dir = Path(cls.tmp) / "archive"
        cls.jsonl_root = Path(cls.tmp) / "jsonl"
        cls.archive_dir.mkdir(parents=True, exist_ok=True)
        cls.jsonl_root.mkdir(parents=True, exist_ok=True)
        os.environ["QUEUE_JSON"] = str(cls.queue_actual)
        os.environ["AGENT_STATE_JSON"] = str(Path(cls.tmp) / "no-agents.json")
        os.environ["AGENTS_JSONL_ROOT"] = str(cls.jsonl_root)
        os.environ["QUEUE_LOG_ARCHIVE_DIR"] = str(cls.archive_dir)
        os.environ["WORKLOAD_LOG_DIR"] = str(Path(cls.tmp) / "no-workloads")
        os.environ["COMPLETED_TASKS_JSONL"] = str(Path(cls.tmp) / "no-completed.jsonl")
        os.environ["HOSTJOB_LOG_DIR"] = str(Path(cls.tmp) / "no-hostjobs")
        os.environ["AGENT_QUEUE_BINDINGS_JSON"] = str(Path(cls.tmp) / "no-bindings.json")
        os.environ["QUEUE_MINISITE_AGENT_STATS_FILE"] = ""

        sys.path.insert(0, str(HERE))
        for mod in list(sys.modules):
            if mod in ("app", "claude_agents"):
                del sys.modules[mod]
        import app as appmod  # noqa: E402

        cls.appmod = appmod
        cls.client = appmod.app.test_client()
        cls.css = (HERE / "static" / "style.css").read_text(encoding="utf-8")
        cls.js = (HERE / "static" / "refresh.js").read_text(encoding="utf-8")
        cls.tpl = (HERE / "templates" / "index.html").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.appmod._cache.fetched_at = 0.0
        self.appmod._MODEL_CACHE.clear()

    # -- helpers ----------------------------------------------------------
    def _write_queue(self, items: list[dict]) -> None:
        self.queue_actual.parent.mkdir(parents=True, exist_ok=True)
        with open(self.queue_actual, "w") as f:
            json.dump(
                {"schema_version": 3, "items": items, "locked_scopes": {}}, f
            )
        self.appmod._cache.fetched_at = 0.0

    def _api(self) -> dict:
        r = self.client.get("/api/queue")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def _html(self) -> str:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        return r.data.decode("utf-8", errors="replace")

    def _row(self, payload: dict, qid: str) -> dict:
        """The shaped row for ``qid`` from whichever section holds it."""
        for key, rows in payload.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and row.get("id") == qid:
                    return row
        self.fail(f"{qid} in no section of the payload")

    def _card(self, html: str, qid: str) -> str:
        start = html.index(f'data-queue-id="{qid}"')
        start = html.rindex("<article", 0, start)
        return html[start : html.index("</article>", start)]

    def _head(self, html: str, qid: str) -> str:
        card = self._card(html, qid)
        start = card.index('<header class="item-head">')
        return card[start : card.index("</header>", start)]

    def _seed_archive(self, qid: str, model: str | None) -> str:
        """Write an archived agent transcript for ``qid``; return its name."""
        name = f"{qid}.jsonl"
        lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "go"}})]
        if model is not None:
            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "model": model, "content": []},
                    }
                )
            )
        (self.archive_dir / name).write_text("\n".join(lines) + "\n")
        return name

    def _seed_live_transcript(self, session_id: str, agent_id: str, model: str) -> None:
        d = self.jsonl_root / session_id / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"agent-{agent_id}.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "agentId": agent_id,
                    "message": {"role": "assistant", "model": model, "content": []},
                }
            )
            + "\n"
        )

    def _css_block(self, selector: str) -> str:
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(m, selector)
        return m.group(1)

    # -- the field on the shaped row --------------------------------------
    def test_stamped_model_decorates_every_section(self):
        """A model is surfaced on rows in EVERY status section, not just
        running ones: "what ran this?" is asked of finished work too."""
        self._write_queue(
            [
                _item(f"q-2026-09-01-{i:04x}", status, model="claude-opus-5")
                for i, status in enumerate(SECTION_STATUSES)
            ]
        )
        payload = self._api()
        for i, status in enumerate(SECTION_STATUSES):
            qid = f"q-2026-09-01-{i:04x}"
            with self.subTest(status=status):
                row = self._row(payload, qid)
                self.assertEqual(row["model"], "claude-opus-5")
                self.assertEqual(row["model_label"], "opus")

    def test_model_read_from_archived_transcript(self):
        qid = "q-2026-09-01-a001"
        name = self._seed_archive(qid, "claude-sonnet-5")
        self._write_queue([_item(qid, "done", log_archive_path=name)])
        row = self._row(self._api(), qid)
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertEqual(row["model_label"], "sonnet")

    def test_model_read_from_live_transcript_of_running_owner(self):
        qid = "q-2026-09-01-a002"
        agent_id = "c1c2d3e4f5a6b7c8d"
        self._seed_live_transcript(
            "cccccccc-0000-0000-0000-00000000000c", agent_id, "claude-fable-5"
        )
        self._write_queue([_item(qid, "running", agent_id=agent_id)])
        row = self._row(self._api(), qid)
        self.assertEqual(row["model"], "claude-fable-5")
        self.assertEqual(row["model_label"], "fable")

    def test_stamped_model_wins_over_transcript(self):
        qid = "q-2026-09-01-a003"
        name = self._seed_archive(qid, "claude-sonnet-5")
        self._write_queue(
            [_item(qid, "done", log_archive_path=name, model="claude-haiku-5")]
        )
        row = self._row(self._api(), qid)
        self.assertEqual(row["model"], "claude-haiku-5")
        self.assertEqual(row["model_label"], "haiku")

    def test_unrecognised_id_keeps_the_raw_id_as_the_label(self):
        """No invented labels: an id from a family this build has never
        heard of is shown verbatim rather than bucketed or dropped."""
        qid = "q-2026-09-01-a004"
        self._write_queue([_item(qid, "done", model="some-future-model-id")])
        row = self._row(self._api(), qid)
        self.assertEqual(row["model"], "some-future-model-id")
        self.assertEqual(row["model_label"], "some-future-model-id")

    def test_absent_model_is_empty_on_every_kind_of_row(self):
        """Pending work (never ran), a workload (ran a shell command, not a
        model) and an agent item whose transcript is gone all resolve to
        ABSENT — no default, no placeholder."""
        rows = [
            _item("q-2026-09-01-b001", "pending"),
            _item(
                "q-2026-09-01-b002",
                "running",
                scope=["workload:some-label"],
            ),
            _item("q-2026-09-01-b003", "done", log_archive_path="q-gone.jsonl"),
            # An archived transcript that never produced an assistant turn
            # (the agent died first) — a real file, no model in it.
            _item(
                "q-2026-09-01-b004",
                "done",
                log_archive_path=self._seed_archive("q-2026-09-01-b004", None),
            ),
        ]
        self._write_queue(rows)
        payload = self._api()
        for rec in rows:
            with self.subTest(qid=rec["id"]):
                row = self._row(payload, rec["id"])
                self.assertEqual(row["model"], "")
                self.assertEqual(row["model_label"], "")

    def test_workload_archive_is_never_scanned_for_a_model(self):
        """A workload's archive is plain stdout, not a transcript: it must
        not be opened looking for `message.model`."""
        qid = "q-2026-09-01-b005"
        (self.archive_dir / f"{qid}.workload.txt").write_text('{"model": "nope"}\n')
        self._write_queue(
            [
                _item(
                    qid,
                    "done",
                    scope=["workload:wl-1"],
                    log_archive_path=f"{qid}.workload.txt",
                )
            ]
        )
        row = self._row(self._api(), qid)
        self.assertEqual(row["model"], "")
        self.assertEqual(row["model_label"], "")
        self.assertTrue(row["has_archive"])

    # -- the rendered chip -------------------------------------------------
    def test_chip_renders_in_the_head_of_every_section(self):
        """In the HEAD specifically — compact density elides the scope and
        description blocks but never the head, which is what keeps the tag
        visible in compact mode."""
        self._write_queue(
            [
                _item(f"q-2026-09-01-{i:04x}", status, model="claude-opus-5")
                for i, status in enumerate(SECTION_STATUSES)
            ]
        )
        html = self._html()
        for i, status in enumerate(SECTION_STATUSES):
            qid = f"q-2026-09-01-{i:04x}"
            with self.subTest(status=status):
                head = self._head(html, qid)
                self.assertIn('class="model-tag"', head)
                self.assertIn(">opus<", head)
                # Raw id on hover — the shorthand is a display convenience,
                # the id is the fact.
                self.assertIn('title="model: claude-opus-5"', head)

    def test_no_chip_and_no_placeholder_when_absent(self):
        qid = "q-2026-09-01-c001"
        self._write_queue([_item(qid, "done")])
        card = self._card(self._html(), qid)
        self.assertNotIn("model-tag", card)
        self.assertNotIn("unknown", card.lower())

    def test_chip_shows_the_raw_id_for_an_unrecognised_family(self):
        qid = "q-2026-09-01-c002"
        self._write_queue([_item(qid, "done", model="some-future-model-id")])
        head = self._head(self._html(), qid)
        self.assertIn(">some-future-model-id<", head)

    # -- renderer parity + compact density ---------------------------------
    def test_refresh_js_mirrors_the_template_macro(self):
        """morphdom replaces the first paint after ~5s: a chip that exists
        only in the Jinja macro would vanish on the first tick."""
        self.assertIn("function modelTag(it)", self.js)
        self.assertIn('class="model-tag"', self.js)
        self.assertIn("{%- macro model_tag(it) -%}", self.tpl)
        # Every card renderer emits it: 6 with a priority chip (running,
        # wedged, quarantined, blocked, pending, other) + the shared
        # done/abandoned terminal renderer.
        self.assertEqual(self.js.count("modelTag(it)"), 8)  # 7 calls + the def
        self.assertEqual(self.tpl.count("{{ model_tag(it) }}"), 8)
        # Both renderers key off the same pre-formatted server field.
        self.assertIn("it.model_label", self.js)

    def test_compact_density_shrinks_but_never_hides_the_chip(self):
        base = self._css_block(".model-tag")
        self.assertIn("font-size", base)
        compact = self._css_block("html.density-compact .model-tag")
        self.assertIn("font-size", compact)
        self.assertNotIn("display", compact)

    # -- cost --------------------------------------------------------------
    def test_archive_scan_is_memoised_across_renders(self):
        """Decorating every row must not re-read every transcript on every
        5s tick — the archived ones are immutable, so scan them once."""
        qid = "q-2026-09-01-d001"
        name = self._seed_archive(qid, "claude-opus-5")
        self._write_queue([_item(qid, "done", log_archive_path=name)])

        real = self.appmod._extract_transcript_model
        calls: list[str] = []

        def counting(path):
            calls.append(str(path))
            return real(path)

        self.appmod._extract_transcript_model = counting
        try:
            for _ in range(3):
                self.appmod._cache.fetched_at = 0.0
                row = self._row(self._api(), qid)
                self.assertEqual(row["model_label"], "opus")
        finally:
            self.appmod._extract_transcript_model = real
        self.assertEqual(len(calls), 1, calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
