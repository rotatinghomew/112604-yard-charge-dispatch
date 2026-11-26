"""在本地重放外部机构和现场设备的合成报文。"""

import argparse
import json
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def load_recording(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def serve_recording(path, port):
    positions = {}
    routes = load_recording(path)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, payload):
            if status == 0:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            content = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def respond(self):
            size = int(self.headers.get("Content-Length", 0))
            if size > 1_048_576:
                self.reply(413, {"错误": "请求体过大"})
                return
            try:
                received = json.loads(self.rfile.read(size)) if size else None
            except ValueError:
                self.reply(400, {"错误": "请求体不是有效 JSON"})
                return

            for index, (method, path, request, replies) in enumerate(routes):
                if (self.command, self.path, received) == (method, path, request):
                    step = positions.get(index, 0)
                    self.reply(*replies[min(step, len(replies) - 1)])
                    positions[index] = step + 1
                    return
            self.reply(404, {"错误": "录制中未找到该项请求"})

        do_GET = respond
        do_POST = respond

        def log_message(self, fmt, *args):
            return

    return HTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", default="samples/peer.json", help="本地 JSON 报文录制")
    parser.add_argument("--port", type=int, default=8087, help="仅监听回环地址")
    options = parser.parse_args()
    with serve_recording(options.recording, options.port) as server:
        print(f"报文重放服务监听 127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
