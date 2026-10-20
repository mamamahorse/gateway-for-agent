"""Actual proxy disconnect monitoring while upstream sends no response bytes."""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import socket
import struct
import threading
import time
import unittest

import test_proxy


class QuietAgent(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        mode = self.path.split('?')[-1]
        entered, closed = self.server.events[mode]
        if self.path.split('?')[0].endswith('/stream') and mode != 'before':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.flush()
        entered.set()
        if select.select([self.connection], [], [], 5)[0]:
            try:
                if self.connection.recv(1) == b'':
                    closed.set()
            except ConnectionResetError:
                closed.set()
        self.close_connection = True


class CancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = ThreadingHTTPServer(('127.0.0.1', 0), QuietAgent)
        cls.backend.daemon_threads = True
        cls.backend.events = {}
        cls.thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.thread.start()
        cls.gateway = test_proxy.GatewayProcess(cls.backend.server_port, settings={
            'GATEWAY_HEADER_TIMEOUT_MS': '1500', 'GATEWAY_MODEL_TIMEOUT_MS': '5000',
            'GATEWAY_STREAM_TIMEOUT_MS': '5000', 'GATEWAY_STREAM_FIRST_BYTE_TIMEOUT_MS': '4000',
            'GATEWAY_ADMISSION_ENABLED': '1', 'GATEWAY_MODEL_CONCURRENCY': '1'})

    @classmethod
    def tearDownClass(cls):
        cls.gateway.stop()
        cls.gateway.errors.seek(0)
        error = cls.gateway.errors.read()
        cls.gateway.close()
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.thread.join(2)
        if error:
            raise AssertionError(error)

    def open(self, mode, stream=True):
        self.backend.events[mode] = (threading.Event(), threading.Event())
        path = '/api/conversations/public/turns' + ('/stream' if stream else '')
        connection = socket.create_connection(('127.0.0.1', self.gateway.port), timeout=3)
        connection.sendall((f'POST {path}?{mode} HTTP/1.1\r\nHost: local\r\n'
            f'X-API-Key: {test_proxy.API_KEY}\r\nContent-Length: 2\r\n\r\n{{}}').encode())
        self.assertTrue(self.backend.events[mode][0].wait(2))
        return connection

    def snapshot(self):
        return json.loads(self.gateway.request('GET', '/metrics')[2])

    def recovered(self):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            stats = self.snapshot()
            if stats['admission']['held_permits'] == 0 and stats['requests']['model_http_in_flight'] == 0:
                return stats
            time.sleep(.01)
        self.fail('proxy model reservation did not return to zero')

    def check_close(self, mode, stream=True, reset=False, half=False):
        connection = self.open(mode, stream)
        start = time.monotonic()
        if reset:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        if half:
            connection.shutdown(socket.SHUT_WR)
        else:
            connection.close()
        try:
            self.assertTrue(self.backend.events[mode][1].wait(1.5), 'upstream socket still open during silent wait')
            self.recovered()
            self.assertLess(time.monotonic() - start, 1.5)
        finally:
            connection.close()

    def test_silent_sse_fin_closes_upstream_without_waiting_for_heartbeat(self):
        self.check_close('sse-fin')

    def test_silent_sse_reset_closes_upstream(self):
        self.check_close('sse-rst', reset=True)

    def test_disconnect_before_response_headers_closes_upstream(self):
        self.check_close('before')

    def test_json_disconnect_closes_upstream(self):
        self.check_close('json-fin', stream=False)

    def test_model_write_half_close_means_withdrawal(self):
        self.check_close('half', half=True)

    def test_disconnect_is_counted_once_and_does_not_reach_total_timeout(self):
        before = self.snapshot()['end_reasons']
        self.check_close('once')
        after = self.snapshot()['end_reasons']
        self.assertEqual(after['client_disconnected'] - before['client_disconnected'], 1)
        self.assertEqual(after['stream_total_timeout'], before['stream_total_timeout'])

    def test_repeated_disconnects_restore_descriptors(self):
        self.recovered()
        pid = self.gateway.process.pid
        baseline = len(list(Path(f'/proc/{pid}/fd').iterdir()))
        for index in range(100):
            self.check_close(f'loop-{index}', stream=index % 2 == 0)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            after = len(list(Path(f'/proc/{pid}/fd').iterdir()))
            if after <= baseline:
                break
            time.sleep(.02)
        self.assertLessEqual(after, baseline)
        print(json.dumps({'disconnects': 100, 'fds_before': baseline, 'fds_after': after}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    args, remaining = parser.parse_known_args()
    test_proxy.BINARY = str(Path(args.binary).resolve(strict=True))
    unittest.main(argv=[__file__, *remaining], verbosity=2)
