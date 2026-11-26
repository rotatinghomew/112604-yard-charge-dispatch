"""End-to-end reconciliation against the recorded pile peer."""

from __future__ import annotations

import unittest
from pathlib import Path
from threading import Thread

from peer_replay import serve_recording
from yard_service import YardService
from yard_service.pile import HttpPileClient


ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "samples" / "peer.json"
SAMPLES = ROOT / "samples"
AT = "2026-11-27T01:42:00Z"


class RecordedPeerFlowTest(unittest.TestCase):
    def test_connection_lost_then_charging_then_stopped(self):
        peer = serve_recording(RECORDING, 0)
        thread = Thread(target=peer.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            client = HttpPileClient(f"http://127.0.0.1:{peer.server_port}", timeout=3)
            service = YardService(SAMPLES, now=AT, pile_client=client)

            result = service.request_stop("S12")
            self.assertIsNotNone(result["transport_error"])
            self.assertEqual(service.session_view("S12")["control_state"], "stop_unconfirmed")

            charging = service.poll_session("S12")
            self.assertEqual(charging["pile_status"], "charging")
            self.assertFalse(charging["stop_confirmed"])

            stopped = service.poll_session("S12")
            self.assertEqual(stopped["pile_status"], "stopped")
            self.assertEqual(stopped["stopped_at"], "2026-11-27T01:42:00Z")
        finally:
            peer.shutdown()
            peer.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
