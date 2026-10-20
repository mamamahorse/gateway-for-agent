"""Synthetic local model which observes EOF while a generation is held open."""
import io
import json
import re
import select
import threading

from knowledge_review_sse import ModelHandler


class CancelModelHandler(ModelHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        if self.path != '/test/stats':
            self.send_error(404)
            return
        with self.server.lock:
            data = json.dumps(self.server.observed).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        marker = re.search(r'cancel-(headers|body|final|timeout|idle)-public-[a-f0-9]+', str(body['messages']))
        mode = marker[1] if marker else None
        identity = marker[0] if marker else None
        final = 'tools' not in body
        hold = mode is not None and (mode != 'final' or final)
        if marker:
            with self.server.lock:
                state = self.server.observed.setdefault(identity, {'calls': 0, 'started': 0, 'closed': 0, 'phase': None})
                state['calls'] += 1
                if hold:
                    state['started'] += 1
                    state['phase'] = 'final' if final else 'agent'
        if not hold:
            # Delegate normal Agent/read_source/structured output handling,
            # restoring the already-read JSON for the shared model fixture.
            previous = self.rfile
            self.rfile = io.BytesIO(raw)
            try:
                return super().do_POST()
            finally:
                self.rfile = previous
        if mode == 'body':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', '4096')
            self.end_headers()
            self.wfile.write(b'{')
            self.wfile.flush()
        if select.select([self.connection], [], [], 8)[0]:
            try:
                ended = self.connection.recv(1) == b''
            except ConnectionResetError:
                ended = True
            if ended:
                with self.server.lock:
                    state['closed'] += 1
        self.close_connection = True


def initialize(server):
    server.requests, server.observed = [], {}
    server.lock, server.release = threading.Lock(), threading.Event()
    server.release.set()
    server.daemon_threads = True
    return server
