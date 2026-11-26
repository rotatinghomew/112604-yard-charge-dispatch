"""通过回环 HTTP 检查报文重放及无应答记录。"""

import http.client
import json
from pathlib import Path
from threading import Thread
import unittest

from peer_replay import load_recording, serve_recording


RECORDING = Path(__file__).resolve().parents[1] / "samples" / "peer.json"


class PeerTest(unittest.TestCase):
    def send(self, port, method, path, request):
        encoded = json.dumps(request).encode() if request is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, path, body=encoded)
            response = connection.getresponse()
            return response.status, json.load(response)
        finally:
            connection.close()

    def test_replay_recording(self):
        with serve_recording(RECORDING, 0) as server:
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                for method, path, request, replies in load_recording(RECORDING):
                    for status, expected in replies:
                        with self.subTest(method=method, path=path, status=status):
                            if status == 0:
                                with self.assertRaises(http.client.RemoteDisconnected):
                                    self.send(server.server_port, method, path, request)
                            else:
                                observed = self.send(server.server_port, method, path, request)
                                self.assertEqual(observed, (status, expected))
                self.assertEqual(self.send(server.server_port, "GET", "/not-recorded", None)[0], 404)
            finally:
                server.shutdown()
                thread.join()
