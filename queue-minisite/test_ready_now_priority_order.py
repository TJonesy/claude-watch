#!/usr/bin/env python3
"""Regression test: `_compute_ready_now` group-head selection must use the
same priority convention as the rest of the codebase -- 1 = highest
priority, so a LOWER `--priority` number outranks a higher one.

Context (Andrew #8886): `_compute_ready_now`'s internal `_sort_key` used to
negate the priority (`-int(priority)`), which treated a HIGHER priority
number as more urgent -- the opposite of session-task's `_sort_key` and of
this same file's own pending-section display sort (`priority asc`, a few
hundred lines down in `_render_payload`). That drift is exactly the bug
class this fix closes: every place priority is compared must agree on
direction.

This test builds two pending items directly (no CLI, `_compute_ready_now`
is a pure function of the items list) in the same group: one at the
default priority 5, one added LATER at priority 1. Priority 1 must win --
it becomes the group head and `ready_now` is True for it, False for the
priority-5 peer.

Run:
    python3 queue-minisite/test_ready_now_priority_order.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _mod in list(sys.modules):
    if _mod in ("app", "claude_agents"):
        del sys.modules[_mod]
import app as appmod  # noqa: E402


def _item(item_id: str, priority: int, created_at: str, group_id: str = "g-shared") -> dict:
    return {
        "id": item_id,
        "summary": f"summary {item_id}",
        "description": "",
        "scope": ["repo:shared"],
        "group_id": group_id,
        "status": "pending",
        "priority": priority,
        "created_by": "main-loop",
        "created_at": created_at,
    }


class ReadyNowPriorityOrderTest(unittest.TestCase):
    def test_priority_1_outranks_priority_5_even_when_added_later(self):
        a = _item("q-a-prio5", priority=5, created_at="2026-09-16T00:00:00+00:00")
        b = _item("q-b-prio1", priority=1, created_at="2026-09-16T00:05:00+00:00")
        items = [a, b]

        self.assertTrue(
            appmod._compute_ready_now(items, b),
            "priority 1 item added later must be ready_now (it outranks priority 5)",
        )
        self.assertFalse(
            appmod._compute_ready_now(items, a),
            "priority 5 item must NOT be ready_now once a priority 1 peer exists",
        )

    def test_equal_priority_falls_back_to_fifo(self):
        a = _item("q-a-first", priority=5, created_at="2026-09-16T00:00:00+00:00")
        b = _item("q-b-second", priority=5, created_at="2026-09-16T00:05:00+00:00")
        items = [a, b]

        self.assertTrue(
            appmod._compute_ready_now(items, a),
            "equal priority: earlier created_at (FIFO) must be ready_now",
        )
        self.assertFalse(appmod._compute_ready_now(items, b))


if __name__ == "__main__":
    unittest.main()
