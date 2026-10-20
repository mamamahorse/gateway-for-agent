"""Real Redis + two actual C++ gateways, including transport fault injection."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from uuid import uuid4

import test_proxy
from fake_agent import make_server

OBSERVATIONS = {}


def wire_command(*args):
    values = [str(arg).encode() if not isinstance(arg, bytes) else arg for arg in args]
    return b"*%d\r\n" % len(values) + b"".join(b"$%d\r\n" % len(v) + v + b"\r\n" for v in values)


def read_resp(reader):
    """Return parsed data and original wire bytes (RESP2 plus HELLO's RESP3 map)."""
    line = reader.readline()
    if not line:
        raise EOFError("Redis socket closed")
    prefix, value = line[:1], line[1:-2]
    if prefix in (b"+", b"-", b":", b"#", b"_", b","):
        return (int(value) if prefix == b":" else value), line
    if prefix in (b"$", b"=", b"!"):
        count = int(value)
        if count == -1:
            return None, line
        body = reader.read(count + 2)
        if len(body) != count + 2:
            raise EOFError("Incomplete Redis body")
        return body[:-2], line + body
    if prefix in (b"*", b"%", b"~", b">", b"|"):
        count = int(value)
        if count == -1:
            return None, line
        items, wire = [], line
        for _ in range(count * (2 if prefix in (b"%", b"|") else 1)):
            item, raw = read_resp(reader)
            items.append(item)
            wire += raw
        return items, wire
    raise ValueError("Unsupported test RESP frame")


class RedisServer:
    def __init__(self):
        if not shutil.which("redis-server"):
            raise RuntimeError("Install redis-server in Ubuntu to run these integration tests")
        self.port = test_proxy.available_port()
        self.directory = tempfile.TemporaryDirectory(prefix="gateway-redis-")
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(["redis-server", "--bind", "127.0.0.1", "--port", str(self.port),
            "--save", "", "--appendonly", "no", "--dir", self.directory.name], stdout=self.log, stderr=self.log)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                if self.command("PING") == b"PONG":
                    return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("Disposable Redis did not start")

    def command(self, *args):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            sock.sendall(wire_command(*args))
            with sock.makefile("rb") as reader:
                return read_resp(reader)[0]

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)

    def close(self):
        self.stop()
        self.log.close()
        self.directory.cleanup()

    def restart(self):
        self.stop()
        self.process = subprocess.Popen(self.process.args, stdout=self.log, stderr=self.log)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                if self.command("PING") == b"PONG":
                    return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("Disposable Redis restart failed")


class FaultHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(4)
        try:
            with socket.create_connection(("127.0.0.1", self.server.redis_port), timeout=4) as upstream, \
                 upstream.makefile("rb") as upstream_reader, self.request.makefile("rb") as reader:
                while True:
                    command, raw = read_resp(reader)
                    upstream.sendall(raw)
                    _, reply = read_resp(upstream_reader)
                    if command[0].upper() == b"EVAL":
                        self.server.eval_count += 1
                        if self.server.mode == "drop":
                            return  # The real EVAL committed; the gateway never saw its result.
                        if self.server.mode == "wrong":
                            reply = b"*3\r\n:9\r\n:0\r\n:100\r\n"
                        if self.server.mode == "oversized":
                            reply = b"*1000000000\r\n"
                        if self.server.mode == "stall":
                            self.server.release.wait(3)
                            return
                    self.request.sendall(reply)
        except (OSError, EOFError, ValueError):
            pass


class FaultProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, mode):
        super().__init__(("127.0.0.1", 0), FaultHandler)
        self.redis_port, self.mode, self.eval_count = port, mode, 0
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=3)


class RateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = make_server()
        # The default five-entry http.server backlog is unrelated to the
        # limiter and can drop an admitted burst before the backend accepts it.
        cls.backend.socket.listen(128)
        cls.backend_thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.backend_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.backend_thread.join(timeout=3)

    def setUp(self):
        self.redis = RedisServer()
        self.gateways = []
        self.proxies = []
        self.prefix = "test:" + uuid4().hex
        self.backend.requests.clear()

    def tearDown(self):
        for gateway in self.gateways:
            gateway.close()
        for proxy in self.proxies:
            proxy.close()
        self.redis.close()

    def gateway(self, **settings):
        config = {"GATEWAY_RATE_LIMIT_ENABLED": "1", "GATEWAY_REDIS_PORT": str(self.redis.port),
                  "GATEWAY_REDIS_TIMEOUT_MS": "500", "GATEWAY_RATE_LIMIT_PREFIX": self.prefix,
                  "GATEWAY_RATE_LIMIT_WINDOW_MS": "5000", "GATEWAY_RATE_LIMIT_NORMAL": "7",
                  "GATEWAY_RATE_LIMIT_MODEL": "3", "GATEWAY_RATE_LIMIT_UPLOAD": "1"}
        config["GATEWAY_REQUEST_TIMEOUT_MS"] = "2500"
        config.update(settings)
        gateway = test_proxy.GatewayProcess(self.backend.server_port, settings=config)
        self.gateways.append(gateway)
        return gateway

    def key(self, category="normal", identity="default"):
        return self.prefix + ":" + identity + ":" + category

    def request(self, gateway, method="GET", target="/api/documents", body=None, **kwargs):
        return gateway.request(method, target, body, **kwargs)

    def test_01_two_gateways_share_one_atomic_quota(self):
        gateways = [self.gateway(), self.gateway()]
        with ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(lambda n: self.request(gateways[n % 2]), range(40)))
        statuses = [row[0] for row in responses]
        self.assertEqual(statuses.count(200), 7, statuses)
        self.assertEqual(statuses.count(429), 33)
        self.assertEqual(self.redis.command("GET", self.key()), b"7")
        self.assertGreater(self.redis.command("PTTL", self.key()), 0)
        self.assertEqual(len(self.backend.requests), 7)
        for status, headers, body in responses:
            if status == 429:
                self.assertGreaterEqual(int(dict(headers)["Retry-After"]), 1)
                self.assertEqual(json.loads(body)["error"]["code"], "RATE_LIMITED")
        OBSERVATIONS["two_gateways"] = {"arrivals": 40, "accepted": 7, "rejected": 33}

    def test_02_window_expiry_and_denials_do_not_refresh_ttl(self):
        gateway = self.gateway(GATEWAY_RATE_LIMIT_NORMAL="1", GATEWAY_RATE_LIMIT_WINDOW_MS="1100")
        self.assertEqual(self.request(gateway)[0], 200)
        first_ttl = self.redis.command("PTTL", self.key())
        status, headers, _ = self.request(gateway)
        self.assertEqual(status, 429)
        self.assertEqual(dict(headers)["Retry-After"], "2")
        time.sleep(0.18)
        self.assertEqual(self.request(gateway)[0], 429)
        self.assertLess(self.redis.command("PTTL", self.key()), first_ttl - 100)
        time.sleep(1)
        self.assertEqual(self.request(gateway)[0], 200)

    def test_03_classes_are_independent_and_stream_denied_before_headers(self):
        gateway = self.gateway(GATEWAY_RATE_LIMIT_NORMAL="1", GATEWAY_RATE_LIMIT_MODEL="1")
        self.assertEqual(self.request(gateway)[0], 200)
        self.assertEqual(self.request(gateway)[0], 429)
        self.assertEqual(self.request(gateway, "POST", "/api/ask", b"{}")[0], 201)
        status, headers, body = self.request(gateway, "POST", "/api/conversations/public/turns/stream", b"{}")
        self.assertEqual(status, 429)
        self.assertTrue(dict(headers)["Content-Type"].startswith("application/json"))
        self.assertNotIn(b"event:", body)
        self.assertEqual(self.request(gateway, "POST", "/api/documents", b"public", headers={"Content-Type":"multipart/form-data; boundary=x"})[0], 201)
        self.assertEqual([self.redis.command("GET", self.key(c)) for c in ["normal","model","upload"]], [b"1"]*3)
        with socket.create_connection(("127.0.0.1", gateway.port), timeout=3) as sock:
            sock.sendall(b"POST /api/documents HTTP/1.1\r\nHost: local\r\nX-API-Key: " + test_proxy.API_KEY.encode() +
                         b"\r\nContent-Type: multipart/form-data; boundary=x\r\nContent-Length: 100\r\nExpect: 100-continue\r\n\r\n")
            response = sock.recv(4096)
        self.assertTrue(response.startswith(b"HTTP/1.1 429"), response)
        self.assertNotIn(b"100 Continue", response)

    def test_04_auth_route_and_header_rejections_do_not_consume_quota(self):
        gateway = self.gateway()
        self.assertEqual(self.request(gateway, auth=False)[0], 401)
        self.assertEqual(self.request(gateway, target="/unknown")[0], 404)
        self.assertEqual(self.request(gateway, headers={"Expect":"100-continue"})[0], 417)
        self.assertEqual(self.request(gateway, "POST", "/api/documents", headers={"Content-Type":"text/plain"})[0], 415)
        self.assertEqual(self.redis.command("KEYS", self.prefix + "*"), [])

    def test_05_health_checks_ping_without_counting_and_key_ids_are_public(self):
        gateway = self.gateway()
        for _ in range(5):
            self.assertEqual(self.request(gateway, target="/livez", auth=False)[0], 200)
            self.assertEqual(self.request(gateway, target="/readyz", auth=False)[0], 200)
        self.assertEqual(self.redis.command("KEYS", self.prefix + "*"), [])
        self.assertEqual(self.request(gateway)[0], 200)
        keys = self.redis.command("KEYS", self.prefix + "*")
        self.assertEqual(keys, [self.key().encode()])
        self.assertNotIn(test_proxy.API_KEY.encode(), b"".join(keys))

    def test_06_redis_unavailable_fails_closed_and_live_stays_available(self):
        gateway = self.gateway()
        self.redis.stop()
        for target in ["/api/documents", "/readyz"]:
            status, _, body = self.request(gateway, target=target, auth=target != "/readyz")
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["error"]["code"], "REDIS_UNAVAILABLE")
        self.assertEqual(self.request(gateway, target="/livez", auth=False)[0], 200)
        self.assertEqual(self.backend.requests, [])

    def test_07_slow_redis_is_async_bounded_and_recovers(self):
        gateway = self.gateway(GATEWAY_REDIS_TIMEOUT_MS="100")
        self.redis.command("CLIENT", "PAUSE", 600, "ALL")
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.request, gateway) for _ in range(8)]
            self.assertEqual(self.request(gateway, target="/livez", auth=False)[0], 200)
            responses = [future.result(timeout=3) for future in futures]
        self.assertTrue(all(row[0] == 503 for row in responses))
        self.assertLess(time.monotonic() - start, 0.6)
        time.sleep(0.7)
        self.assertEqual(self.request(gateway)[0], 200)

    def test_08_orphan_counters_get_ttl_even_if_already_exhausted(self):
        gateway = self.gateway()
        self.redis.command("SET", self.key(), 7)
        self.assertEqual(self.request(gateway)[0], 429)
        self.assertGreater(self.redis.command("PTTL", self.key()), 0)
        self.redis.command("SET", self.key(), 2)
        self.assertEqual(self.request(gateway)[0], 200)
        self.assertEqual(self.redis.command("GET", self.key()), b"3")
        self.assertGreater(self.redis.command("PTTL", self.key()), 0)

    def test_09_invalid_redis_state_is_not_exposed_or_forwarded(self):
        gateway = self.gateway()
        for value in ["private-state", "-1", "1.5"]:
            self.redis.command("SET", self.key(), value)
            status, _, body = self.request(gateway)
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["error"], {"code":"REDIS_UNAVAILABLE", "message":"Rate limit service is unavailable"})
        self.assertEqual(self.backend.requests, [])

    def test_10_distinct_public_key_ids_have_separate_quotas(self):
        one = self.gateway(GATEWAY_RATE_LIMIT_NORMAL="1", GATEWAY_KEY_ID="one")
        two = self.gateway(GATEWAY_RATE_LIMIT_NORMAL="1", GATEWAY_KEY_ID="two")
        self.assertEqual([self.request(g)[0] for g in [one,one,two,two]], [200,429,200,429])

    def test_11_committed_eval_lost_reply_is_never_replayed(self):
        proxy = FaultProxy(self.redis.port, "drop")
        self.proxies.append(proxy)
        gateway = self.gateway(GATEWAY_REDIS_PORT=str(proxy.server_address[1]))
        self.assertEqual(self.request(gateway)[0], 503)
        time.sleep(0.15)
        self.assertEqual(proxy.eval_count, 1)
        self.assertEqual(self.redis.command("GET", self.key()), b"1")
        self.assertEqual(self.backend.requests, [])

    def test_12_invalid_reply_and_stalled_reply_fail_closed(self):
        for mode in ["wrong", "stall", "oversized"]:
            proxy = FaultProxy(self.redis.port, mode)
            self.proxies.append(proxy)
            gateway = self.gateway(GATEWAY_REDIS_PORT=str(proxy.server_address[1]), GATEWAY_REDIS_TIMEOUT_MS="150")
            self.assertEqual(self.request(gateway)[0], 503)
        self.assertEqual(self.backend.requests, [])

    def test_13_operations_reclaim_fds_and_shutdown_under_stall(self):
        gateway = self.gateway(GATEWAY_RATE_LIMIT_NORMAL="1000")
        process = Path(f"/proc/{gateway.process.pid}")
        self.request(gateway)
        time.sleep(0.1)
        before = len(list((process / "fd").iterdir()))
        threads_before = len(list((process / "task").iterdir()))
        for _ in range(100):
            self.assertEqual(self.request(gateway)[0], 200)
        time.sleep(0.15)
        after = len(list((process / "fd").iterdir()))
        self.assertLessEqual(after, before + 1)
        self.assertEqual(len(list((process / "task").iterdir())), threads_before)
        OBSERVATIONS["redis_checks"] = {"count":100,"fds_before":before,"fds_after":after,"threads":threads_before}
        proxy = FaultProxy(self.redis.port, "stall")
        self.proxies.append(proxy)
        stalled = self.gateway(GATEWAY_REDIS_PORT=str(proxy.server_address[1]), GATEWAY_REDIS_TIMEOUT_MS="5000")
        sock = socket.create_connection(("127.0.0.1", stalled.port), timeout=2)
        try:
            sock.sendall(b"GET /api/documents HTTP/1.1\r\nHost: local\r\nX-API-Key: " + test_proxy.API_KEY.encode() + b"\r\n\r\n")
            time.sleep(0.1)
            start = time.monotonic()
            stalled.stop()
            self.assertLess(time.monotonic() - start, 1)
        finally:
            sock.close()

    def test_14_redis_restart_resets_cache_and_recovers_without_gateway_restart(self):
        gateway = self.gateway(GATEWAY_RATE_LIMIT_NORMAL="1")
        self.assertEqual(self.request(gateway)[0], 200)
        self.assertEqual(self.request(gateway)[0], 429)
        self.redis.restart()
        self.assertIsNone(self.redis.command("GET", self.key()))
        self.assertEqual(self.request(gateway)[0], 200)
        self.assertEqual(self.request(gateway, target="/readyz", auth=False)[0], 200)

    def test_15_explicitly_disabled_mode_does_not_depend_on_redis(self):
        gateway = self.gateway(GATEWAY_RATE_LIMIT_ENABLED="0")
        self.redis.stop()
        self.assertEqual(self.request(gateway)[0], 200)
        self.assertEqual(self.request(gateway, target="/readyz", auth=False)[0], 200)

    def test_16_invalid_settings_fail_without_echoing_secrets(self):
        for settings in [{"GATEWAY_RATE_LIMIT_ENABLED":"true"}, {"GATEWAY_REDIS_TIMEOUT_MS":"0"},
                         {"GATEWAY_REDIS_PORT":"65536"}, {"GATEWAY_REDIS_HOST":"private://secret"},
                         {"GATEWAY_KEY_ID":"private:secret"}, {"GATEWAY_RATE_LIMIT_PREFIX":"private/secret"},
                         {"GATEWAY_RATE_LIMIT_NORMAL":"1000001"}]:
            result = subprocess.run([test_proxy.BINARY], env={"PATH":os.environ["PATH"],
                "GATEWAY_API_KEY":test_proxy.API_KEY, **settings}, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(test_proxy.API_KEY.encode(), result.stderr)
            self.assertNotIn(b"private", result.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    args, rest = parser.parse_known_args()
    test_proxy.BINARY = args.binary
    result = unittest.main(argv=[__file__, *rest], verbosity=2, exit=False)
    print("LIMITER_OBSERVATIONS=" + json.dumps(OBSERVATIONS), flush=True)
    raise SystemExit(not result.result.wasSuccessful())
