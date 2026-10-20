"""Black-box counters, bounded JSON/SSE observation, and live admission gauges."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import os
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from fake_agent import FakeAgentHandler
from test_rate_limit import RedisServer
import test_proxy

OBSERVATIONS = {}


class Handler(FakeAgentHandler):
    def handle_request(self):
        case = parse_qs(urlsplit(self.path).query).get('case', [''])[0]
        if case not in self.server.replies and case not in self.server.gates:
            return super().handle_request()
        body = b''.join(self.body_blocks())
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        if case in self.server.gates:
            with self.server.condition:
                self.server.entered.append(case)
                self.server.condition.notify_all()
            self.server.gates[case].wait(8)
            return self.reply(200, b'{}')
        spec = self.server.replies[case]
        if not spec.get('stream'):
            return self.reply(spec.get('status', 200), spec['body'], content_type=spec.get('type', 'application/json'))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        self.close_connection = True
        try:
            pieces = spec.get('pieces', [spec['body']])
            for part in pieces:
                self.write_chunk(part)
                if spec.get('delay'):
                    time.sleep(spec['delay'])
            if not spec.get('abrupt'):
                self.wfile.write(b'0\r\n\r\n')
        except OSError:
            pass


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.backend.socket.listen(128)
        self.backend.health_status = 200
        self.backend.requests, self.backend.upload_progress = [], {}
        self.backend.replies, self.backend.gates, self.backend.entered = {}, {}, []
        self.backend.condition = threading.Condition()
        self.thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.thread.start()
        self.gateway = None
        self.redis = None
        self.pool = ThreadPoolExecutor(max_workers=8)

    def tearDown(self):
        for event in self.backend.gates.values():
            event.set()
        self.pool.shutdown(wait=True)
        if self.gateway:
            self.gateway.stop()
            self.gateway.errors.seek(0)
            errors = self.gateway.errors.read()
            self.assertEqual(self.gateway.process.returncode, 0, errors)
            self.assertEqual(errors, '')
            self.gateway.close()
        if self.redis:
            self.redis.close()
        self.backend.shutdown()
        self.backend.server_close()
        self.thread.join(3)

    def start(self, **settings):
        self.gateway = test_proxy.GatewayProcess(self.backend.server_port, settings=settings)

    def snapshot(self):
        status, headers, body = self.gateway.request('GET', '/metrics')
        self.assertEqual(status, 200)
        self.assertEqual(dict(headers)['Cache-Control'], 'no-store')
        self.assertLess(len(body), 32768)
        return json.loads(body)

    def finished(self, count):
        end = time.monotonic() + 3
        while True:
            value = self.snapshot()
            if value['requests']['finished'] >= count:
                return value
            self.assertLess(time.monotonic(), end)
            time.sleep(.01)

    def reply(self, case, body, **spec):
        self.backend.replies[case] = {'body': body, **spec}
        path = '/api/conversations/public/turns/stream' if spec.get('stream') else '/api/ask'
        return self.gateway.request('POST', f'{path}?case={case}', b'{}')

    def hold(self, name):
        self.backend.gates[name] = threading.Event()
        return self.pool.submit(self.gateway.request, 'POST', f'/api/ask?case={name}', b'{}')

    def wait_entered(self, count):
        with self.backend.condition:
            self.assertTrue(self.backend.condition.wait_for(lambda: len(self.backend.entered) >= count, timeout=3))

    def test_01_key_required_and_probes_do_not_pollute_request_counters(self):
        self.start()
        self.assertEqual(self.gateway.request('GET', '/metrics', auth=False)[0], 401)
        self.assertEqual(self.gateway.request('GET', '/metrics', headers={'X-API-Key': 'wrong'})[0], 401)
        before = self.snapshot()['requests']
        for _ in range(3):
            self.assertEqual(self.gateway.request('GET', '/livez', auth=False)[0], 200)
            self.assertEqual(self.gateway.request('GET', '/readyz', auth=False)[0], 200)
            self.snapshot()
        self.assertEqual(self.snapshot()['requests'], before)
        self.assertEqual(before['finished'], 0)
        self.assertFalse(any(path.startswith('/metrics') for _, path, _, _ in self.backend.requests))

    def test_02_status_reasons_bytes_and_no_private_labels(self):
        self.start()
        self.assertEqual(self.gateway.request('GET', '/api/documents?private-query-marker')[0], 200)
        self.assertEqual(self.gateway.request('GET', '/unknown-private-route')[0], 404)
        self.assertEqual(self.gateway.request('POST', '/api/ask', b'{}', auth=False)[0], 401)
        value = self.finished(3)
        self.assertEqual(value['requests']['http_status'], {'200': 1, '401': 1, '404': 1})
        self.assertEqual(value['end_reasons']['auth_rejected'], 1)
        self.assertEqual(value['end_reasons']['route_rejected'], 1)
        self.assertGreater(value['requests']['response_body_bytes_total'], 0)
        text = json.dumps(value)
        for marker in (test_proxy.API_KEY, 'private-query-marker', 'unknown-private-route'):
            self.assertNotIn(marker, text)

    def test_03_http_200_failed_json_is_not_business_success(self):
        self.start()
        body = b'{"status":"failed","error":"private-response-marker"}'
        status, _, received = self.reply('json-failed', body)
        self.assertEqual((status, received), (200, body))
        value = self.finished(1)
        self.assertEqual(value['requests']['http_status']['200'], 1)
        self.assertEqual(value['requests']['observed_business_failures'], 1)
        self.assertNotIn('private-response-marker', json.dumps(value))

    def test_04_nested_and_string_mentions_are_not_top_level_failures(self):
        self.start()
        for case, body in [('nested', b'{"answer":{"status":"failed"}}'),
                           ('mentioned', b'{"answer":"status: failed"}'),
                           ('escaped', b'{"\\u0073tatus":"fa\\u0069led"}')]:
            self.assertEqual(self.reply(case, body)[2], body)
        self.assertEqual(self.finished(3)['requests']['observed_business_failures'], 1)

    def test_05_sse_fragments_bom_and_all_line_endings_count_once(self):
        self.start()
        for index, separator in enumerate((b'\n', b'\r\n', b'\r')):
            lines = [b'\xef\xbb\xbf: ping', b'event: progress', b'data: {"status":"failed"}', b'',
                     b'event:error', 'data: {"message":"中文"}'.encode(), b'',
                     b'event:done', b'data:{"status":"failed"}', b'', b'']
            body = separator.join(lines)
            status, _, received = self.reply(f'fragment-{index}', body, stream=True,
                                            pieces=[body[i:i+1] for i in range(len(body))], delay=.0005)
            self.assertEqual((status, received), (200, body))
        value = self.finished(3)['requests']
        self.assertEqual(value['observed_business_failures'], 3)
        self.assertEqual(value['streams_without_done'], 0)
        self.assertEqual(value['business_inspection_unavailable'], 0)

    def test_06_multiline_done_and_progress_are_distinguished(self):
        self.start()
        body = (b'event: progress\ndata: {"status":"failed"}\n\n'
                b'event: done\ndata: {"status":\ndata: "failed"}\n\n')
        self.assertEqual(self.reply('multiline', body, stream=True)[2], body)
        self.assertEqual(self.finished(1)['requests']['observed_business_failures'], 1)

    def test_07_large_sse_result_is_forwarded_without_hiding_later_error(self):
        self.start()
        body = (b'event: result\ndata: {"public":"' + b'x' * (2 * 1024 * 1024) + b'"}\n\n'
                b'event: error\ndata: {"code":"PUBLIC"}\n\n'
                b'event: done\ndata: {"status":"failed"}\n\n')
        status, _, received = self.reply('large-stream', body, stream=True,
                                        pieces=[body[i:i+16384] for i in range(0, len(body), 16384)])
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(received).hexdigest(), hashlib.sha256(body).hexdigest())
        value = self.finished(1)['requests']
        self.assertEqual(value['observed_business_failures'], 1)
        self.assertEqual(value['streams_without_done'], 0)
        OBSERVATIONS['large_sse_bytes_preserved'] = len(body)

    def test_08_large_or_invalid_json_is_unknown_and_unchanged(self):
        self.start()
        for case, body in [('large-json', b'{"status":"failed","public":"' + b'x' * 70000 + b'"}'),
                           ('invalid-json', b'{"status":"failed",')]:
            self.assertEqual(self.reply(case, body)[2], body)
        value = self.finished(2)['requests']
        self.assertEqual(value['observed_business_failures'], 0)
        self.assertEqual(value['business_inspection_unavailable'], 2)

    def test_09_malformed_done_or_no_done_is_not_success(self):
        self.start()
        body = b'event: done\ndata: malformed\n\n'
        self.assertEqual(self.reply('bad-done', body, stream=True)[2], body)
        value = self.finished(1)['requests']
        self.assertEqual(value['observed_business_failures'], 0)
        self.assertEqual(value['business_inspection_unavailable'], 1)
        self.assertEqual(value['streams_without_done'], 1)

    def test_10_abrupt_stream_tracks_transport_failure_and_missing_done(self):
        self.start()
        with self.assertRaises(http.client.IncompleteRead):
            self.reply('abrupt', b'event: progress\ndata: {}\n\n', stream=True, abrupt=True)
        value = self.finished(1)
        self.assertEqual(value['requests']['http_status'], {'200': 1})
        self.assertEqual(value['requests']['streams_without_done'], 1)
        self.assertEqual(value['end_reasons']['upstream_stream_error'], 1)

    def test_11_live_admission_gauges_and_overload_then_recovery(self):
        self.start(GATEWAY_ADMISSION_ENABLED='1', GATEWAY_MODEL_CONCURRENCY='2',
                   GATEWAY_MODEL_QUEUE_LIMIT='1', GATEWAY_MODEL_QUEUE_TIMEOUT_MS='2000',
                   GATEWAY_MODEL_TIMEOUT_MS='5000')
        active = [self.hold('active-0'), self.hold('active-1')]
        self.wait_entered(2)
        queued = self.hold('queued')
        end = time.monotonic() + 2
        while True:
            value = self.snapshot()
            if value['admission']['queued'] == 1:
                break
            self.assertLess(time.monotonic(), end)
            time.sleep(.01)
        self.assertEqual(value['admission']['held_permits'], 2)
        self.assertEqual(value['admission']['queued_body_bytes'], 2)
        self.assertEqual(value['requests']['model_http_in_flight'], 3)
        self.assertEqual(self.gateway.request('POST', '/api/ask', b'{}')[0], 503)
        self.assertEqual(self.finished(1)['end_reasons']['queue_full'], 1)
        self.backend.gates['active-0'].set()
        self.wait_entered(3)
        for event in self.backend.gates.values():
            event.set()
        self.assertTrue(all(f.result(3)[0] == 200 for f in active + [queued]))
        value = self.finished(4)
        self.assertEqual(value['requests']['model_http_in_flight'], 0)
        self.assertEqual(value['admission']['held_permits'], 0)
        self.assertEqual(value['admission']['queued'], 0)
        self.assertEqual(value['admission']['queued_body_bytes'], 0)
        OBSERVATIONS['busy_gauges_return_to_zero'] = True

    def test_12_queue_timeout_and_request_deadline_have_separate_reasons(self):
        self.start(GATEWAY_ADMISSION_ENABLED='1', GATEWAY_MODEL_CONCURRENCY='1',
                   GATEWAY_MODEL_QUEUE_TIMEOUT_MS='100', GATEWAY_MODEL_TIMEOUT_MS='5000')
        active = self.hold('active')
        self.wait_entered(1)
        self.assertEqual(self.gateway.request('POST', '/api/ask', b'{}')[0], 503)
        value = self.finished(1)
        self.assertEqual(value['end_reasons']['queue_timeout'], 1)
        self.assertEqual(value['end_reasons']['timeout'], 0)
        self.assertEqual(value['admission']['queued'], 0)
        self.backend.gates['active'].set()
        self.assertEqual(active.result(3)[0], 200)

    def test_13_snapshot_stays_available_during_redis_outage_and_429(self):
        self.redis = RedisServer()
        self.start(GATEWAY_RATE_LIMIT_ENABLED='1', GATEWAY_REDIS_PORT=str(self.redis.port),
                   GATEWAY_REDIS_TIMEOUT_MS='300', GATEWAY_RATE_LIMIT_NORMAL='1',
                   GATEWAY_RATE_LIMIT_PREFIX='test:metrics')
        self.assertEqual(self.gateway.request('GET', '/api/documents')[0], 200)
        self.assertEqual(self.gateway.request('GET', '/api/documents')[0], 429)
        self.assertEqual(self.finished(2)['end_reasons']['rate_limited'], 1)
        self.redis.stop()
        self.assertEqual(self.gateway.request('GET', '/readyz', auth=False)[0], 503)
        self.assertEqual(self.snapshot()['requests']['finished'], 2)
        self.assertEqual(self.gateway.request('GET', '/api/documents')[0], 503)
        value = self.finished(3)
        self.assertEqual(value['end_reasons']['redis_unavailable'], 1)
        self.assertEqual(value['requests']['http_status'], {'200': 1, '429': 1, '503': 1})

    def test_14_upstream_http_error_connect_error_and_timeout(self):
        self.start(GATEWAY_REQUEST_TIMEOUT_MS='100')
        self.assertEqual(self.reply('http-error', b'{"detail":"public"}', status=422)[0], 422)
        self.assertEqual(self.gateway.request('GET', '/api/documents?case=slow')[0], 504)
        self.backend.shutdown()
        self.backend.server_close()
        self.assertEqual(self.gateway.request('GET', '/api/documents')[0], 502)
        value = self.finished(3)
        self.assertEqual(value['requests']['upstream_http_errors'], 1)
        self.assertEqual(value['end_reasons']['timeout'], 1)
        self.assertEqual(value['end_reasons']['upstream_error'], 1)

    def test_15_repeated_snapshot_is_bounded_and_recovers_descriptors(self):
        self.start()
        body = b'{"question":"private-body-marker"}'
        self.assertEqual(self.gateway.request('POST', '/api/ask?private-url-marker', body)[0], 201)
        before = self.finished(1)
        baseline = len(os.listdir(f'/proc/{self.gateway.process.pid}/fd'))
        for _ in range(100):
            value = self.snapshot()
            self.assertEqual(value['requests'], before['requests'])
            for marker in (test_proxy.API_KEY, 'private-body-marker', 'private-url-marker'):
                self.assertNotIn(marker, json.dumps(value))
        end = time.monotonic() + 1
        while len(os.listdir(f'/proc/{self.gateway.process.pid}/fd')) > baseline:
            self.assertLess(time.monotonic(), end)
            time.sleep(.01)
        after = len(os.listdir(f'/proc/{self.gateway.process.pid}/fd'))
        OBSERVATIONS['snapshot_polling'] = {'count': 100, 'fds_before': baseline, 'fds_after': after}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args, remaining = parser.parse_known_args()
    test_proxy.BINARY = os.path.abspath(args.binary)
    result = unittest.main(argv=[__file__, *remaining], exit=False, verbosity=2)
    print(json.dumps({'observations': OBSERVATIONS}), flush=True)
    raise SystemExit(0 if result.result.wasSuccessful() else 1)
