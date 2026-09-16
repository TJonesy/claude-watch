#!/usr/bin/env python3
"""Tests that the q-site Stop / Abandon buttons ACTUALLY KILL a hostjob worker
(Andrew #8650/#8667).

Before this fix ``/api/queue/stop`` and ``/api/queue/abandon`` only shelled out
to ``session-task queue abandon`` -- which flips the queue ROW but kills
nothing. A hostjob has no owning agent and no obligations gate, so the worker
kept running forever. ``_do_abandon`` now ALSO asks the host-side broker to
SIGTERM/SIGKILL the worker (``_hostjob_broker_stop``), and it accepts a hostjob
row in EITHER running or pending state (a live worker can sit pending after a
lost register race / ``--no-queue``).

These are unit tests: ``_read_queue`` (used both for the scope lookup and, via
``_ids_by_status``, for eligibility), the broker stop, session-task preflight,
and the abandon subprocess are all stubbed, so no real queue / broker /
session-task is needed.

Run::

    python3 queue-minisite/test_hostjob_stop_kill.py
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


class _Proc:
    def __init__(self, rc=0, out="ok", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class HostjobStopKillTest(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        self._queue = {
            "items": [
                {
                    "id": "q-runjob",
                    "status": "running",
                    "scope": ["hostjob:runjob"],
                    "summary": "hj run",
                },
                {
                    "id": "q-pend",
                    "status": "pending",
                    "scope": ["hostjob:pendjob"],
                    "summary": "hj pend",
                },
                {
                    "id": "q-agent",
                    "status": "running",
                    "scope": ["repo:x"],
                    "summary": "agent task",
                },
            ]
        }
        self.stops = []
        self._orig = {
            "read": appmod._read_queue,
            "stop": appmod._hostjob_broker_stop,
            "pre": appmod._session_task_preflight,
            "run": appmod.subprocess.run,
        }
        appmod._read_queue = lambda: (self._queue, None)

        def _fake_stop(label):
            self.stops.append(label)
            return {"reached": True, "ok": True, "detail": {"label": label}}

        appmod._hostjob_broker_stop = _fake_stop
        appmod._session_task_preflight = lambda: None
        appmod.subprocess.run = lambda *a, **k: _Proc()
        appmod._cache.fetched_at = 0.0

    def tearDown(self):
        appmod._read_queue = self._orig["read"]
        appmod._hostjob_broker_stop = self._orig["stop"]
        appmod._session_task_preflight = self._orig["pre"]
        appmod.subprocess.run = self._orig["run"]

    def test_stop_running_hostjob_kills_worker(self):
        r = self.client.post("/api/queue/stop", json={"id": "q-runjob", "reason": "x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        b = r.get_json()
        self.assertTrue(b["ok"], b)
        self.assertEqual(self.stops, ["runjob"])
        self.assertEqual(b["hostjob_label"], "runjob")
        self.assertTrue(b["hostjob_stop"]["ok"])
        self.assertEqual(b["kill_mechanism"], "hostjob-stop+abandon")

    def test_pending_hostjob_is_stoppable(self):
        # A --no-queue / register-race hostjob sits PENDING but is live; the
        # Stop button (allowed_statuses=("running",)) must still accept it and
        # kill the worker.
        r = self.client.post("/api/queue/stop", json={"id": "q-pend", "reason": "x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.stops, ["pendjob"])

    def test_abandon_pending_hostjob_kills_worker(self):
        r = self.client.post("/api/queue/abandon", json={"id": "q-pend"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.stops, ["pendjob"])
        b = r.get_json()
        self.assertEqual(b["kill_mechanism"], "hostjob-stop+abandon")

    def test_non_hostjob_stop_does_not_call_broker(self):
        r = self.client.post("/api/queue/stop", json={"id": "q-agent", "reason": "x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.stops, [])
        b = r.get_json()
        self.assertEqual(b["kill_mechanism"], "abandon-only")

    def test_broker_unreachable_still_abandons_but_flags_worker(self):
        appmod._hostjob_broker_stop = lambda label: {
            "reached": False,
            "ok": False,
            "error": "boom",
        }
        r = self.client.post("/api/queue/stop", json={"id": "q-runjob", "reason": "x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        b = r.get_json()
        self.assertTrue(b["ok"], b)  # the row still flips
        self.assertFalse(b["hostjob_stop"]["ok"])
        self.assertIn("may still be", b["kill_note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
