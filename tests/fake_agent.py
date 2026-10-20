"""Public, synthetic backend for transport tests. It never calls an AI service."""

import json
import hashlib
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeAgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, body, extra_headers=(), content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_request(self):
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        case = query.get("case", [""])[0]
        if path.path == "/api/documents" and self.command == "POST":
            if case == "reject_upload":
                self.close_connection = True
                return self.reply(413, b'{"detail":"synthetic early upload rejection"}')
            digest = hashlib.sha256()
            total = 0
            for block in self.body_blocks():
                digest.update(block)
                total += len(block)
                self.server.upload_progress[self.path] = total
            self.server.requests.append((self.command, self.path, dict(self.headers), b""))
            result = {"bytes": total, "sha256": digest.hexdigest(), "content_type": self.headers.get("Content-Type"),
                      "transfer_encoding": self.headers.get("Transfer-Encoding")}
            return self.reply(201, json.dumps(result).encode())
        body = b"".join(self.body_blocks())
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        if path.path == "/healthz":
            return self.reply(self.server.health_status, b'{"status":"ok"}')
        if case == "slow":
            time.sleep(1.2)
        if case == "abrupt":
            self.send_response(200)
            self.send_header("Content-Length", "80")
            self.end_headers()
            self.wfile.write(b'{"partial":')
            self.close_connection = True
            return
        if case == "huge":
            self.send_response(200)
            self.send_header("Content-Length", str(5 * 1024 * 1024))
            self.end_headers()
            self.close_connection = True
            return
        if path.path.endswith("/turns/stream"):
            if case == "status":
                return self.reply(429, b'{"detail":"synthetic upstream limit"}', [("Retry-After", "2")])
            if case == "status_event":
                return self.reply(429, b'event: error\ndata: {"code":"LIMITED"}\n\n',
                                  [("Retry-After", "2")], content_type="text/event-stream")
            if case == "wrong_type":
                return self.reply(200, b'{"not":"a stream"}')
            return self.stream_events(case)
        if case == "stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(b"event: meta\ndata: {}\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        if case == "chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "X-Backend-Hop, close")
            self.send_header("X-Backend-Hop", "must-not-leak")
            self.send_header("Set-Cookie", "first=1")
            self.send_header("Set-Cookie", "second=2")
            self.end_headers()
            try:
                for chunk in (b'{"text":"', "中文".encode(), b'"}'):
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        if case == "failed":
            return self.reply(200, b'{"status":"failed","error":"synthetic failure"}')
        if case == "status":
            return self.reply(422, b'{"detail":"synthetic validation error"}')
        if self.command == "POST":
            return self.reply(201, body, [("X-Upstream", "fake-agent")])
        self.reply(200, json.dumps({"path": self.path, "headers": dict(self.headers)}, ensure_ascii=False).encode())

    def body_blocks(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                remaining = int(line.split(b";", 1)[0], 16)
                if remaining == 0:
                    while self.rfile.readline() not in (b"\r\n", b"", b"\n"):
                        pass
                    return
                while remaining:
                    block = self.rfile.read(min(remaining, 16384))
                    if not block:
                        return
                    remaining -= len(block)
                    yield block
                if self.rfile.read(2) != b"\r\n":
                    return
        else:
            remaining = int(self.headers.get("Content-Length", "0"))
            while remaining:
                block = self.rfile.read(min(remaining, 16384))
                if not block:
                    return
                remaining -= len(block)
                yield block

    def stream_events(self, case):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "X-Stream-Hop, close")
        self.send_header("X-Stream-Hop", "remove-me")
        if case == "encoded":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.close_connection = True
        try:
            if case == "first_delay":
                time.sleep(1)
            if case == "flood":
                block = b"data: " + b"x" * (16384 - 8) + b"\n\n"
                for _ in range(4096):
                    self.write_chunk(block)
            elif case == "heartbeat":
                for _ in range(30):
                    self.write_chunk(b": ping\n\n")
                    time.sleep(0.1)
            elif case == "utf8":
                data = b'data: {"text":"' + "中文".encode() + b'"}\n\nevent: done\ndata: {}\n\n'
                for byte in data:
                    self.write_chunk(bytes([byte]))
            else:
                for index in range(3):
                    self.write_chunk(f'event: progress\ndata: {{"seq":{index}}}\n\n'.encode())
                    if case == "abrupt_stream":
                        return # No final HTTP chunk: deliberately incomplete message.
                    if case == "stall":
                        time.sleep(1)
                    else:
                        time.sleep(0.25)
                self.write_chunk(b'event: done\ndata: {"status":"completed"}\n\n')
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def write_chunk(self, data):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()


def make_server(port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeAgentHandler)
    server.daemon_threads = True
    server.health_status = 200
    server.requests = []
    server.upload_progress = {}
    return server


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    server = make_server(parser.parse_args().port)
    print(f"Synthetic backend: http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
