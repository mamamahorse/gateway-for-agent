"""Real gateway admission against an event-controlled HTTP/SSE backend."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import test_proxy

OBSERVATIONS = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'{}')

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        query = parse_qs(urlsplit(self.path).query)
        token = query['token'][0]
        with self.server.condition:
            self.server.entered.append(token)
            self.server.active += 1
            self.server.peak = max(self.server.peak, self.server.active)
            self.server.condition.notify_all()
        try:
            self.server.gates[token].wait(15)
            if 'abrupt' in query:
                self.close_connection = True
                return
            stream = urlsplit(self.path).path.endswith('/stream')
            body = b'event: meta\ndata: {}\n\nevent: done\ndata: {}\n\n' if stream else b'{}'
            self.send_response(422 if 'reject' in query else 200)
            self.send_header('Content-Type', 'text/event-stream' if stream else 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, BrokenPipeError):
            pass
        finally:
            with self.server.condition:
                self.server.active -= 1
                self.server.condition.notify_all()


class Backend(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.condition = threading.Condition()
        self.entered, self.gates = [], {}
        self.active = self.peak = 0
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def wait_entered(self, count):
        with self.condition:
            if not self.condition.wait_for(lambda: len(self.entered) >= count, timeout=5):
                raise AssertionError(('upstream admission timeout', count, self.entered))


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.gateway = None
        self.clients = []
        self.pool = ThreadPoolExecutor(max_workers=16)

    def tearDown(self):
        for gate in self.backend.gates.values():
            gate.set()
        for client in self.clients:
            client.close()
        self.pool.shutdown(wait=True)
        if self.gateway:
            self.gateway.stop()
            self.gateway.errors.seek(0)
            errors = self.gateway.errors.read()
            self.assertEqual(self.gateway.process.returncode, 0, errors)
            self.assertEqual(errors, '')
            self.gateway.close()
        self.backend.shutdown()
        self.backend.server_close()

    def start(self, **settings):
        env = {'GATEWAY_ADMISSION_ENABLED': '1', 'GATEWAY_MODEL_CONCURRENCY': '2',
               'GATEWAY_MODEL_QUEUE_LIMIT': '8', 'GATEWAY_MODEL_QUEUE_TIMEOUT_MS': '4000',
               'GATEWAY_MODEL_TIMEOUT_MS': '10000', 'GATEWAY_STREAM_TIMEOUT_MS': '10000'}
        env.update(settings)
        self.gateway = test_proxy.GatewayProcess(self.backend.server_port, settings=env)

    def path(self, token, path='/api/ask', suffix=''):
        self.backend.gates[token] = threading.Event()
        return f'{path}?token={token}{suffix}'

    def send(self, token, path='/api/ask', suffix=''):
        target = self.path(token, path, suffix)
        return self.pool.submit(self.gateway.request, 'POST', target, b'{}')

    def raw(self, token, size=2):
        target = self.path(token)
        client = socket.create_connection(('127.0.0.1', self.gateway.port), timeout=3)
        body = b'{}' if size == 2 else b' ' * size
        client.sendall((f'POST {target} HTTP/1.1\r\nHost: localhost\r\nX-API-Key: {test_proxy.API_KEY}\r\n'
                        f'Content-Length: {size}\r\n\r\n').encode() + body)
        self.clients.append(client)
        return client

    def rows(self):
        # pread does not move the file position shared with the writing child.
        data = os.pread(self.gateway.log.fileno(), 2 * 1024 * 1024, 0).decode()
        return [json.loads(line) for line in data.splitlines() if line.endswith('}')]

    def wait_queue(self, size):
        end = time.monotonic() + 4
        while time.monotonic() < end:
            if any(row.get('event') == 'admission' and row['queued'] == size for row in self.rows()):
                return
            time.sleep(.01)
        self.fail(('queue did not reach size', size, self.rows()))

    def test_01_two_active_eight_fifo_and_overload(self):
        self.start()
        active = [self.send(f'a{i}') for i in range(2)]
        self.backend.wait_entered(2)
        queued = []
        for i in range(8):
            queued.append(self.send(f'q{i}'))
            self.wait_queue(i + 1)
        rejected = self.send('rejected').result(3)
        self.assertEqual(rejected[0], 503)
        self.assertEqual(json.loads(rejected[2])['error']['code'], 'GATEWAY_OVERLOADED')
        self.assertEqual(dict(rejected[1])['Retry-After'], '1')
        self.assertEqual(len(self.backend.entered), 2)
        self.assertEqual(self.gateway.request('GET', '/api/documents')[0], 200)
        self.assertEqual(self.gateway.request('GET', '/readyz', auth=False)[0], 200)
        self.backend.gates['a0'].set()
        for i in range(8):
            self.backend.wait_entered(i + 3)
            self.assertEqual(self.backend.entered[-1], f'q{i}')
            self.backend.gates[f'q{i}'].set()
        self.backend.gates['a1'].set()
        self.assertTrue(all(future.result(5)[0] == 200 for future in active + queued))
        self.assertEqual(self.backend.peak, 2)
        OBSERVATIONS['capacity'] = {'peak_upstream': self.backend.peak, 'queued': 8,
                                    'overload_status': rejected[0], 'fifo': self.backend.entered[2:]}

    def test_02_json_and_sse_share_one_slot(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1')
        first = self.send('json')
        self.backend.wait_entered(1)
        second = self.send('sse', '/api/conversations/chat/turns/stream')
        self.wait_queue(1)
        self.assertEqual(self.backend.entered, ['json'])
        self.backend.gates['json'].set()
        self.backend.wait_entered(2)
        self.backend.gates['sse'].set()
        self.assertEqual(first.result(5)[0], 200)
        self.assertIn(b'event: done', second.result(5)[2])
        self.assertEqual(self.backend.peak, 1)

    def test_03_queue_timeout_does_not_call_upstream_and_recovers(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1', GATEWAY_MODEL_QUEUE_LIMIT='1',
                   GATEWAY_MODEL_QUEUE_TIMEOUT_MS='150')
        first = self.send('active')
        self.backend.wait_entered(1)
        start = time.monotonic()
        status, _, body = self.send('timeout').result(3)
        elapsed = time.monotonic() - start
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)['error']['code'], 'GATEWAY_OVERLOADED')
        self.assertGreaterEqual(elapsed, .12)
        self.assertLess(elapsed, 2)
        self.assertEqual(self.backend.entered, ['active'])
        next_request = self.send('next')
        self.backend.gates['active'].set()
        self.backend.wait_entered(2)
        self.assertEqual(self.backend.entered[-1], 'next')
        self.backend.gates['next'].set()
        self.assertEqual(first.result(5)[0], 200)
        self.assertEqual(next_request.result(5)[0], 200)

    def test_04_queued_disconnect_reclaims_item_and_bytes(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1', GATEWAY_MODEL_QUEUE_LIMIT='1',
                   GATEWAY_MODEL_QUEUE_BYTES='2')
        first = self.send('active')
        self.backend.wait_entered(1)
        client = self.raw('detached')
        self.wait_queue(1)
        client.close()
        end = time.monotonic() + 3
        while not any(row.get('reason') == 'queue_client_closed' for row in self.rows()):
            self.assertLess(time.monotonic(), end)
            time.sleep(.01)
        next_request = self.send('next')
        self.backend.gates['active'].set()
        self.backend.wait_entered(2)
        self.assertEqual(self.backend.entered, ['active', 'next'])
        self.backend.gates['next'].set()
        self.assertEqual(first.result(5)[0], 200)
        self.assertEqual(next_request.result(5)[0], 200)

    def test_05_queue_byte_budget_rejects_before_count_limit(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1', GATEWAY_MODEL_QUEUE_BYTES='3')
        first = self.send('active')
        self.backend.wait_entered(1)
        self.raw('queued')
        self.wait_queue(1)
        self.assertEqual(self.send('over-byte-budget').result(3)[0], 503)
        self.assertEqual(self.backend.entered, ['active'])
        self.backend.gates['queued'].set()
        self.backend.gates['active'].set()
        self.assertEqual(first.result(5)[0], 200)

    def test_06_upstream_failure_releases_permit(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1')
        for i, suffix in enumerate(('&reject=1', '&abrupt=1', '')):
            future = self.send(f'failure-{i}', suffix=suffix)
            self.backend.wait_entered(i + 1)
            self.backend.gates[f'failure-{i}'].set()
            self.assertEqual(future.result(5)[0], (422, 502, 200)[i])

    def test_07_active_timeout_releases_only_proxy_permit(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1', GATEWAY_MODEL_TIMEOUT_MS='150')
        future = self.send('active')
        self.backend.wait_entered(1)
        self.assertEqual(future.result(3)[0], 504)
        next_request = self.send('next')
        self.backend.wait_entered(2)
        # The synthetic upstream keeps the old work running: a proxy permit
        # alone is not an actual business-task concurrency guarantee.
        self.assertEqual(self.backend.active, 2)
        self.backend.gates['next'].set()
        self.backend.gates['active'].set()
        self.assertEqual(next_request.result(3)[0], 200)

    def test_08_shutdown_with_active_and_queued_has_no_leak(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1')
        self.raw('active')
        self.backend.wait_entered(1)
        self.raw('queued')
        self.wait_queue(1)
        start = time.monotonic()
        self.gateway.stop()
        self.assertLess(time.monotonic() - start, 3)

    def test_09_repeated_queue_disconnects_recover_fds(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1', GATEWAY_MODEL_QUEUE_LIMIT='1')
        future = self.send('active')
        self.backend.wait_entered(1)
        baseline = len(os.listdir(f'/proc/{self.gateway.process.pid}/fd'))
        for i in range(100):
            client = self.raw(f'cancel-{i}')
            before = sum(row.get('reason') == 'queue_client_closed' for row in self.rows())
            client.close()
            end = time.monotonic() + 2
            while sum(row.get('reason') == 'queue_client_closed' for row in self.rows()) == before:
                self.assertLess(time.monotonic(), end)
                time.sleep(.01)
        after = len(os.listdir(f'/proc/{self.gateway.process.pid}/fd'))
        self.assertLessEqual(after, baseline + 1)
        OBSERVATIONS['queue_disconnects'] = {'count': 100, 'fds_before': baseline, 'fds_after': after}
        self.backend.gates['active'].set()
        self.assertEqual(future.result(5)[0], 200)

    def test_10_queued_write_half_close_means_withdrawal(self):
        self.start(GATEWAY_MODEL_CONCURRENCY='1')
        future = self.send('active')
        self.backend.wait_entered(1)
        client = self.raw('half-closed')
        self.wait_queue(1)
        client.shutdown(socket.SHUT_WR)
        self.assertEqual(client.recv(1), b'')
        self.assertEqual(self.backend.entered, ['active'])
        self.backend.gates['active'].set()
        self.assertEqual(future.result(5)[0], 200)

    def test_11_bad_capacity_configuration_fails_without_echoing_values(self):
        for name, value in [('GATEWAY_ADMISSION_ENABLED', 'private-config-marker'),
                            ('GATEWAY_MODEL_CONCURRENCY', '0'), ('GATEWAY_MODEL_QUEUE_LIMIT', '129'),
                            ('GATEWAY_MODEL_QUEUE_BYTES', '99999999999'),
                            ('GATEWAY_MODEL_QUEUE_TIMEOUT_MS', '-1')]:
            with self.subTest(name=name):
                result = subprocess.run([test_proxy.BINARY], env={'PATH': os.environ['PATH'],
                    'GATEWAY_API_KEY': test_proxy.API_KEY, name: value},
                    capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertNotIn('private-config-marker', result.stderr)
                self.assertNotIn(test_proxy.API_KEY, result.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args, remaining = parser.parse_known_args()
    test_proxy.BINARY = args.binary
    result = unittest.main(argv=[__file__, *remaining], exit=False, verbosity=2)
    print(json.dumps({'observations': OBSERVATIONS}), flush=True)
    raise SystemExit(0 if result.result.wasSuccessful() else 1)
