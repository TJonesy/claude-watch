#!/usr/bin/env python3
"""Test the hostjob broker's cross-boundary STOP endpoint (Andrew #8650/#8667).

The queue-minisite runs in a container and cannot os.kill() a host worker pid
(different PID namespace), so its Stop button POSTs to the broker -- a host-side
singleton in the host PID namespace -- at ``POST /stop/<label>``, which calls
``hostjob``'s own ``_stop_one``. This exercises that route end to end with
``_stop_one`` stubbed (the real one signals live pids, out of scope here).

Run::

    python3 -m pytest test_hostjob_broker_stop_endpoint.py -v
    # or
    python3 test_hostjob_broker_stop_endpoint.py
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOSTJOB = HERE.parent / "hostjob"


def _load_hostjob():
    loader = SourceFileLoader("hostjob_stop_under_test", str(HOSTJOB))
    spec = importlib.util.spec_from_loader("hostjob_stop_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Args:
    def __init__(self, port):
        self.port = port


class BrokerStopEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_hostjob()
        cls.port = _free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(
            target=cls.mod.cmd_broker, args=(_Args(cls.port),), daemon=True
        )
        cls.thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.base + "/healthz", timeout=1) as r:
                    if r.read() == b"ok":
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError("broker did not come up on %s" % cls.base)

    def setUp(self):
        self._orig_stop = self.mod._stop_one

    def tearDown(self):
        self.mod._stop_one = self._orig_stop

    def _post_stop(self, path):
        req = urllib.request.Request(self.base + path, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())

    def test_stop_endpoint_calls_stop_one_with_parsed_args(self):
        calls = []

        def fake(label, grace, force):
            calls.append((label, grace, force))
            return 0, "hostjob[%s]: stopped" % label

        self.mod._stop_one = fake
        status, body = self._post_stop("/stop/myjob?grace=3&force=1")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["label"], "myjob")
        self.assertEqual(calls, [("myjob", 3, True)])

    def test_stop_endpoint_defaults_grace_and_force(self):
        calls = []
        self.mod._stop_one = lambda label, grace, force: (
            calls.append((label, grace, force)) or (0, "ok")
        )
        status, body = self._post_stop("/stop/plainjob")
        self.assertEqual(status, 200)
        self.assertEqual(calls, [("plainjob", 5, False)])

    def test_stop_endpoint_nonzero_returns_500(self):
        self.mod._stop_one = lambda label, grace, force: (1, "no such job")
        req = urllib.request.Request(self.base + "/stop/gone", data=b"", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTP 500")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 500)
            body = json.loads(e.read())
            self.assertFalse(body["ok"])
            self.assertEqual(body["label"], "gone")

    def test_stop_endpoint_stop_one_exception_does_not_crash_broker(self):
        def boom(label, grace, force):
            raise RuntimeError("kaboom")

        self.mod._stop_one = boom
        req = urllib.request.Request(self.base + "/stop/errjob", data=b"", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTP 500")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 500)
            body = json.loads(e.read())
            self.assertFalse(body["ok"])
        # Broker still serves after the exception.
        with urllib.request.urlopen(self.base + "/healthz", timeout=2) as r:
            self.assertEqual(r.read(), b"ok")

    def test_ingest_still_works_after_stop_route_added(self):
        # Regression: adding /stop must not break /ingest (both are do_POST).
        req = urllib.request.Request(
            self.base + "/ingest/ingestjob", data=b"hello\n", method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            self.assertIn(r.status, (200, 204))


if __name__ == "__main__":
    unittest.main(verbosity=2)
