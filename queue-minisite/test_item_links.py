#!/usr/bin/env python3
"""Tests for per-item links (chat / issues / related) and linkified text.

`session-task queue add/update/links` write an optional `links` field onto a
queue item. The minisite renders it on every card:

* a Chat pill and an Issues row (``.item-links``), kept in compact density;
* a folded ``Links (N)`` disclosure for related URLs, with a kind badge;
* bare http(s) URLs in the Prompt body and the blocker line as anchors.

Load-bearing details these tests pin:

* Every anchor is ``target="_blank" rel="noopener noreferrer"``.
* queue.json is re-validated at render time: a non-http(s) URL in `links`
  never reaches an href, whatever wrote it.
* Linkified text is escaped piecewise: markup in a description stays text.
* Items written before `links` existed render exactly as before and get the
  empty shape in ``/api/queue``.
* static/refresh.js mirrors the template (morphdom replaces the first paint
  within 5s). The LINKIFY_CASES below are duplicated verbatim in
  static/links.test.js, which runs them through the JS linkify().

Run::

    python3 queue-minisite/test_item_links.py
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

CHAT = "https://chat.example/#/room/!abc:example.org"
ISSUE = "https://git.example/org/foo/issues/12"
PR = "https://git.example/org/foo/pulls/34"
A = 'target="_blank" rel="noopener noreferrer"'

# (input, expected linkify() output). Kept free of quote characters so the
# Jinja escaper (&#34;) and refresh.js esc() (leaves quotes in text) agree
# byte-for-byte; the quote case is asserted structurally instead.
LINKIFY_CASES = [
    ("see https://git.example/o/r/pulls/7.",
     f'see <a class="qlink autolink" href="https://git.example/o/r/pulls/7" {A}>'
     'https://git.example/o/r/pulls/7</a>.'),
    ("<b>x</b> (https://x.example/?a=1&b=2)",
     f'&lt;b&gt;x&lt;/b&gt; (<a class="qlink autolink" href="https://x.example/?a=1&amp;b=2" {A}>'
     'https://x.example/?a=1&amp;b=2</a>)'),
    ("javascript:alert(1) https:///nohost ftp://x.example/",
     "javascript:alert(1) https:///nohost ftp://x.example/"),
    ("HTTP://Upper.example/x\nnext line",
     f'<a class="qlink autolink" href="HTTP://Upper.example/x" {A}>'
     'HTTP://Upper.example/x</a>\nnext line'),
]


def _item(item_id: str, status: str, **extra) -> dict:
    d = {
        "id": item_id,
        "summary": f"summary {item_id}",
        "description": "",
        "scope": [],
        "status": status,
        "priority": 5,
        "created_by": "main-loop",
        "created_at": "2026-06-01T00:00:00+00:00",
    }
    d.update(extra)
    return d


FULL_LINKS = {
    "chat": CHAT,
    "issues": [{"url": ISSUE, "label": None},
               {"url": "https://gitlab.example/g/sub/proj/-/issues/3",
                "label": "deploy flake"}],
    "related": [
        {"url": PR, "label": None, "kind": "pr"},
        {"url": "https://terrakube.example/app/org/ws/7?x=1", "label": None,
         "kind": "terrakube"},
    ],
}


class ItemLinksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="qmin-item-links-")
        cls.queue_actual = Path(cls.tmp) / ".config/session/queue.json"
        os.environ["QUEUE_JSON"] = str(cls.queue_actual)
        os.environ["AGENT_STATE_JSON"] = str(Path(cls.tmp) / "no-agents.json")
        os.environ["AGENTS_JSONL_ROOT"] = str(Path(cls.tmp) / "no-jsonl")
        os.environ["QUEUE_LOG_ARCHIVE_DIR"] = str(Path(cls.tmp) / "no-archive")
        os.environ["WORKLOAD_LOG_DIR"] = str(Path(cls.tmp) / "no-workloads")

        sys.path.insert(0, str(HERE))
        for mod in list(sys.modules):
            if mod in ("app", "claude_agents"):
                del sys.modules[mod]
        import app as appmod  # noqa: E402

        cls.appmod = appmod
        cls.client = appmod.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _write(self, items):
        self.queue_actual.parent.mkdir(parents=True, exist_ok=True)
        with open(self.queue_actual, "w") as f:
            json.dump({"schema_version": 3, "items": items,
                       "locked_scopes": {}}, f)
        self.appmod._cache.fetched_at = 0.0

    def setUp(self):
        self._write([
            _item("q-pend", "pending", links=FULL_LINKS,
                  description=f"Fix it. PR: {PR}. <script>x</script>"),
            _item("q-blk", "blocked", links={"chat": CHAT},
                  block_reason=f"waiting on {ISSUE}, then merge",
                  blocked_at="2026-06-02T00:00:00+00:00"),
            _item("q-old", "pending", description="legacy, no links"),
            _item("q-evil", "pending", links={
                "chat": "javascript:alert(1)",
                "issues": ["data:text/html,hi", {"url": " "}],
                "related": [{"url": "https://ok.example/a", "kind": "<b>"},
                            {"url": "vbscript:x"}],
            }),
            _item("q-done", "done", links={"related": [{"url": PR}]},
                  completed_at="2026-06-03T00:00:00+00:00"),
            _item("q-wedge", "wedged", links={"chat": CHAT},
                  wedged_at="2026-06-03T00:00:00+00:00"),
        ])

    # ---------- helpers ----------

    def _html(self) -> str:
        return self.client.get("/").data.decode("utf-8", errors="replace")

    def _card(self, html: str, item_id: str) -> str:
        start = html.find(f'data-queue-id="{item_id}"')
        self.assertNotEqual(start, -1, f"no card for {item_id}")
        start = html.rfind("<article", 0, start)
        end = html.find("</article>", start)
        return html[start:end]

    def _api_item(self, item_id: str) -> dict:
        payload = json.loads(self.client.get("/api/queue").data)
        for key, val in payload.items():
            if isinstance(val, list):
                for it in val:
                    if isinstance(it, dict) and it.get("id") == item_id:
                        return it
        self.fail(f"{item_id} not in /api/queue")

    # ---------- rendering ----------

    def test_chat_issues_and_links_render(self):
        card = self._card(self._html(), "q-pend")
        self.assertIn(
            f'<a class="qlink chat-link" href="{CHAT}" {A} title="{CHAT}">Chat</a>',
            card)
        self.assertIn('<span class="links-label">Issues</span>', card)
        self.assertIn(f'href="{ISSUE}" {A} title="{ISSUE}">org/foo#12</a>', card)
        self.assertIn(">deploy flake</a>", card)
        self.assertIn('<summary class="prompt-summary">Links (2)</summary>', card)
        self.assertIn('<span class="link-kind">pr</span>', card)
        self.assertIn(">org/foo#34</a>", card)
        self.assertIn(">terrakube.example/app/org/ws/7…</a>", card)
        # Every anchor in a links block opens safely in a new tab.
        for tag in re.findall(r"<a class=\"qlink[^>]*>", card):
            self.assertIn(A, tag)

    def test_links_row_precedes_age_and_list_precedes_prompt(self):
        card = self._card(self._html(), "q-pend")
        self.assertLess(card.find('class="item-links"'), card.find('class="age"'))
        self.assertLess(card.find("links-toggle"), card.find("Prompt ("))

    def test_every_section_renders_links(self):
        html = self._html()
        self.assertIn("chat-link", self._card(html, "q-blk"))
        self.assertIn("chat-link", self._card(html, "q-wedge"))
        self.assertIn("links-toggle", self._card(html, "q-done"))

    def test_legacy_item_renders_no_links_markup(self):
        card = self._card(self._html(), "q-old")
        self.assertNotIn("item-links", card)
        self.assertNotIn("links-toggle", card)
        self.assertEqual(
            self._api_item("q-old")["links"],
            {"chat": None, "issues": [], "related": []})

    def test_unsafe_urls_never_reach_an_href(self):
        html = self._html()
        card = self._card(html, "q-evil")
        for bad in ("javascript:", "data:text", "vbscript:"):
            self.assertNotIn(bad, card)
        self.assertNotIn("chat-link", card)
        self.assertIn('<span class="link-kind">&lt;b&gt;</span>', card)
        links = self._api_item("q-evil")["links"]
        self.assertIsNone(links["chat"])
        self.assertEqual(links["issues"], [])
        self.assertEqual([e["url"] for e in links["related"]],
                         ["https://ok.example/a"])

    def test_api_queue_carries_links(self):
        links = self._api_item("q-pend")["links"]
        self.assertEqual(links["chat"], CHAT)
        self.assertEqual(links["issues"][0],
                         {"url": ISSUE, "label": None, "text": "org/foo#12"})
        self.assertEqual(links["issues"][1]["text"], "deploy flake")
        self.assertEqual(links["related"][0]["kind"], "pr")

    # ---------- linkify ----------

    def test_description_is_linkified_and_escaped(self):
        card = self._card(self._html(), "q-pend")
        self.assertIn(
            f'PR: <a class="qlink autolink" href="{PR}" {A}>{PR}</a>. '
            "&lt;script&gt;x&lt;/script&gt;", card)
        self.assertNotIn("<script>", card)

    def test_block_reason_is_linkified(self):
        card = self._card(self._html(), "q-blk")
        self.assertIn(
            f'<strong>blocker:</strong> waiting on <a class="qlink autolink" '
            f'href="{ISSUE}" {A}>{ISSUE}</a>, then merge', card)

    def test_linkify_cases(self):
        for text, want in LINKIFY_CASES:
            with self.subTest(text=text):
                self.assertEqual(str(self.appmod._linkify(text)), want)

    def test_linkify_quote_cannot_break_out_of_href(self):
        out = str(self.appmod._linkify('https://x.example/"onmouseover="alert(1)'))
        self.assertEqual(out.count("<a "), 1)
        self.assertIn('href="https://x.example/"', out)
        self.assertNotIn('" onmouseover', out)
        self.assertNotIn("onmouseover=\"alert", out.split("</a>")[0])

    # ---------- SPA mirror + click wiring ----------

    def test_refresh_js_mirrors_template(self):
        js = (HERE / "static/refresh.js").read_text()
        self.assertEqual(js.count("linksRow(it) +"), 7)
        self.assertEqual(js.count("linksList(it) +"), 7)
        self.assertIn("${linkify(it.block_reason)}", js)
        self.assertNotIn("${esc(it.description)}</pre>", js)
        # Same regex + trailing rule as app.py.
        self.assertIn(r"""const LINKIFY_RE = /https?:\/\/[^\s<>"'`]+/gi;""", js)
        self.assertIn(f"const LINKIFY_TRAILING = '{self.appmod._LINKIFY_TRAILING}';", js)

    def test_link_clicks_do_not_open_the_card_modal(self):
        live = (HERE / "static/live-log.js").read_text()
        self.assertIn("if (ev.target.closest('a.qlink')) return;", live)
        self.assertIn("if (active.closest('a.qlink')) return;", live)
        kb = (HERE / "static/keyboard.js").read_text()
        self.assertIn("focused.closest('a.qlink')", kb)


if __name__ == "__main__":
    unittest.main(verbosity=2)
