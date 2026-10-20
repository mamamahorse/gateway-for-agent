"""Protected worker metrics and explicit existing background-job routes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import threading
import time
import unittest

from fake_agent import FakeAgentHandler, make_server
from test_rate_limit import RedisServer
import test_proxy


class Agent(FakeAgentHandler):
    def do_PATCH(self):
        return self.handle_request()

    def do_GET(self):
        if self.path == '/internal/metrics':
            self.server.requests.append((self.command, self.path, dict(self.headers), b''))
            time.sleep(self.server.metrics_delay)
            return self.reply(200, b'{"scope":"python_process","held_slots":1}', [('Cache-Control', 'public')])
        return super().do_GET()


class GovernanceProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = make_server()
        cls.backend.RequestHandlerClass = Agent
        cls.backend.metrics_delay = 0
        cls.thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.thread.start()
        cls.gateway = test_proxy.GatewayProcess(cls.backend.server_port,
            settings={'GATEWAY_READINESS_TIMEOUT_MS': '200', 'GATEWAY_HEADER_TIMEOUT_MS': '1000'})

    @classmethod
    def tearDownClass(cls):
        cls.gateway.stop()
        cls.gateway.errors.seek(0)
        errors = cls.gateway.errors.read()
        cls.gateway.close()
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.thread.join(2)
        if errors:
            raise AssertionError(errors)

    def test_agent_metrics_requires_key_and_is_not_a_public_internal_route(self):
        before = len(self.backend.requests)
        for headers in ({}, {'X-API-Key': 'wrong'}):
            status, values, _ = self.gateway.request('GET', '/agent-metrics', auth=False, headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(dict(values)['Cache-Control'], 'no-store')
        self.assertEqual(self.gateway.request('GET', '/internal/metrics')[0], 404)
        self.assertEqual(len(self.backend.requests), before)

    def test_worker_snapshot_is_forwarded_with_fixed_target_and_no_store(self):
        status, headers, body = self.gateway.request('GET', '/agent-metrics?secret-query=discard')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['scope'], 'python_process')
        self.assertEqual(dict(headers)['Cache-Control'], 'no-store')
        _, target, upstream_headers, _ = self.backend.requests[-1]
        self.assertEqual(target, '/internal/metrics')
        self.assertNotIn('X-API-Key', upstream_headers)
        self.assertTrue(upstream_headers['X-Request-ID'].startswith('gw-'))

    def test_body_upgrade_and_wrong_methods_are_rejected(self):
        before = len(self.backend.requests)
        self.assertEqual(self.gateway.request('GET', '/agent-metrics', b'x')[0], 400)
        self.assertEqual(self.gateway.request('GET', '/agent-metrics', headers={
            'Connection': 'Upgrade', 'Upgrade': 'websocket'})[0], 400)
        self.assertEqual(self.gateway.request('POST', '/agent-metrics', b'{}')[0], 404)
        self.assertEqual(len(self.backend.requests), before)

    def test_worker_metric_polls_do_not_count_as_business_requests(self):
        before = json.loads(self.gateway.request('GET', '/metrics')[2])
        for _ in range(5):
            self.assertEqual(self.gateway.request('GET', '/agent-metrics')[0], 200)
        after = json.loads(self.gateway.request('GET', '/metrics')[2])
        self.assertEqual(before['requests'], after['requests'])
        self.assertEqual(before['end_reasons'], after['end_reasons'])

    def test_metrics_bypass_redis_outage_and_exhausted_quota(self):
        redis = RedisServer()
        gateway = None
        try:
            gateway = test_proxy.GatewayProcess(self.backend.server_port, settings={
                'GATEWAY_RATE_LIMIT_ENABLED': '1', 'GATEWAY_REDIS_PORT': str(redis.port),
                'GATEWAY_REDIS_TIMEOUT_MS': '100', 'GATEWAY_RATE_LIMIT_NORMAL': '1',
                'GATEWAY_RATE_LIMIT_MODEL': '1', 'GATEWAY_RATE_LIMIT_PREFIX': 'test:m233:metrics'})
            self.assertEqual(gateway.request('GET', '/api/documents')[0], 200)
            self.assertEqual(gateway.request('GET', '/api/documents')[0], 429)
            self.assertEqual(gateway.request('GET', '/agent-metrics')[0], 200)
            self.assertEqual(gateway.request('GET', '/api/documents')[0], 429)
            redis.stop()
            self.assertEqual(gateway.request('GET', '/api/documents')[0], 503)
            self.assertEqual(gateway.request('GET', '/agent-metrics')[0], 200)
        finally:
            if gateway is not None:
                gateway.close()
            redis.close()

    def test_metrics_bypass_busy_model_admission(self):
        gateway = test_proxy.GatewayProcess(self.backend.server_port, settings={
            'GATEWAY_ADMISSION_ENABLED': '1', 'GATEWAY_MODEL_CONCURRENCY': '1',
            'GATEWAY_MODEL_TIMEOUT_MS': '2500', 'GATEWAY_READINESS_TIMEOUT_MS': '500'})
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(gateway.request, 'POST', '/api/ask?case=slow', '{}')
                end = time.monotonic() + 2
                while json.loads(gateway.request('GET', '/metrics')[2])['admission']['held_permits'] != 1:
                    self.assertLess(time.monotonic(), end)
                    time.sleep(.01)
                start = time.monotonic()
                self.assertEqual(gateway.request('GET', '/agent-metrics')[0], 200)
                self.assertLess(time.monotonic() - start, .5)
                self.assertEqual(future.result(4)[0], 201)
        finally:
            gateway.close()

    def test_slow_or_unavailable_python_does_not_break_local_metrics(self):
        self.backend.metrics_delay = .8
        try:
            status, headers, _ = self.gateway.request('GET', '/agent-metrics')
            self.assertEqual(status, 504)
            self.assertEqual(dict(headers)['Cache-Control'], 'no-store')
            self.assertEqual(self.gateway.request('GET', '/metrics')[0], 200)
        finally:
            self.backend.metrics_delay = 0
        gateway = test_proxy.GatewayProcess(test_proxy.available_port())
        try:
            self.assertEqual(gateway.request('GET', '/agent-metrics')[0], 502)
            self.assertEqual(gateway.request('GET', '/metrics')[0], 200)
        finally:
            gateway.close()

    def test_sixteen_background_support_routes_are_explicit_and_preserve_payloads(self):
        paths = [('POST', '/api/collections'), ('PATCH', '/api/outline-groups/public'),
                 ('GET', '/api/collections/root/outlines'), ('POST', '/api/collections/root/outlines'),
                 ('GET', '/api/outlines/public'), ('POST', '/api/outlines/public/resume'),
                 ('POST', '/api/outlines/public/pause'), ('POST', '/api/outlines/public/grouping'),
                 ('POST', '/api/outline-group-runs/public/retry'), ('POST', '/api/outline-group-runs/public/pause'),
                 ('POST', '/api/outline-groups/public/source-reviews'),
                 ('POST', '/api/outline-source-reviews/public/continue'), ('POST', '/api/outline-source-reviews/public/pause'),
                 ('POST', '/api/outlines/public/routes'), ('POST', '/api/outline-routes/public/continue'),
                 ('POST', '/api/outline-routes/public/pause')]
        for method, path in paths:
            with self.subTest(path=path):
                body = b'{"request_id":"public"}' if method in ('POST', 'PATCH') else None
                status, _, _ = self.gateway.request(method, path, body)
                self.assertIn(status, (200, 201))  # Real 202/state behavior is tested with Uvicorn.
                self.assertEqual(self.backend.requests[-1][1], path)
                self.assertEqual(self.backend.requests[-1][3], body or b'')
        self.assertEqual(self.gateway.request('DELETE', '/api/outlines/public')[0], 404)
        self.assertEqual(self.gateway.request('POST', '/api/outlines/public/unknown', '{}')[0], 404)

    def test_repeated_proxied_snapshot_reads_reclaim_file_descriptors(self):
        pid = self.gateway.process.pid
        self.gateway.request('GET', '/agent-metrics')
        time.sleep(.1)
        before = len(list(Path(f'/proc/{pid}/fd').iterdir()))
        for _ in range(100):
            self.assertEqual(self.gateway.request('GET', '/agent-metrics')[0], 200)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            after = len(list(Path(f'/proc/{pid}/fd').iterdir()))
            if after <= before:
                break
            time.sleep(.02)
        self.assertLessEqual(after, before)
        print(json.dumps({'agent_metrics_polls': 100, 'fds_before': before, 'fds_after': after}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    args, remaining = parser.parse_known_args()
    test_proxy.BINARY = str(Path(args.binary).resolve(strict=True))
    unittest.main(argv=[__file__, *remaining], verbosity=2)
