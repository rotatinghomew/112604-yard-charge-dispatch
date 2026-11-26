"""HTTP smoke tests for the JSON service."""

from __future__ import annotations

import http.client
import json
import unittest
from pathlib import Path
from threading import Thread

from yard_service.server import create_server


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(port=0, data_dir=SAMPLES, now="2026-11-27T01:42:00Z")
        cls.thread = Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def call(self, method, path, body=None):
        encoded = json.dumps(body).encode() if body is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(
                method,
                path,
                body=encoded,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read()
            data = json.loads(raw) if raw else None
            return response.status, data
        finally:
            connection.close()

    def test_health_dashboard_and_blocked_handoff_over_http(self):
        status, data = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, dashboard = self.call("GET", "/api/dashboard?at=2026-11-27T01:42:00Z")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["counts"]["bookings"], 2)

        status, payload = self.call(
            "POST",
            "/api/handoff/collect",
            {"booking": "C-9", "at": "2026-11-27T01:42:00Z"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "whole_group_not_ready")

    def test_generic_action_and_ui(self):
        status, data = self.call(
            "POST",
            "/api/action",
            {"action": "assess_capacity", "at": "2026-01-01T00:00:00Z"},
        )
        # Empty in-memory sample service still has sample bookings; the route
        # itself should be reachable and return an assessment object.
        self.assertEqual(status, 200)
        self.assertIn("decisions", data)

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("整组", html)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
