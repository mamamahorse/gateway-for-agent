"""Black-box tests of the real C++ executable and a controlled HTTP backend."""

import argparse
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from fake_agent import make_server

API_KEY = "synthetic-test-key-only"
BINARY = ""


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GatewayProcess:
    def __init__(self, upstream_port, health_path="/healthz", settings=None):
        self.port = available_port()
        self.log = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.errors = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.environment = {
            "PATH": os.environ["PATH"], "GATEWAY_API_KEY": API_KEY,
            "GATEWAY_PORT": str(self.port),
            "GATEWAY_UPSTREAM": f"http://127.0.0.1:{upstream_port}",
            "GATEWAY_UPSTREAM_HEALTH_PATH": health_path,
            "GATEWAY_HEADER_TIMEOUT_MS": "350", "GATEWAY_REQUEST_TIMEOUT_MS": "600",
            "GATEWAY_MODEL_TIMEOUT_MS": "600", "GATEWAY_READINESS_TIMEOUT_MS": "400",
        }
        for name in ("ASAN_OPTIONS", "UBSAN_OPTIONS"):
            if name in os.environ:
                self.environment[name] = os.environ[name]
        self.environment.update(settings or {})
        self.process = subprocess.Popen([BINARY], env=self.environment, stdout=self.log, stderr=self.errors)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if self.process.poll() is not None:
                self.errors.seek(0)
                raise RuntimeError("Gateway exited before startup: " + self.errors.read())
            try:
                if self.request("GET", "/livez", auth=False)[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.02)
        raise RuntimeError("Gateway startup timed out")

    def request(self, method, path, body=None, *, auth=True, headers=None):
        request_headers = dict(headers or {})
        if auth:
            request_headers.setdefault("X-API-Key", API_KEY)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise

    def close(self):
        self.stop()
        self.log.close()
        self.errors.close()


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = make_server()
        cls.backend_thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.backend_thread.start()
        cls.gateway = GatewayProcess(cls.backend.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.gateway.stop()
        cls.gateway.errors.seek(0)
        errors = cls.gateway.errors.read()
        cls.gateway.log.seek(0)
        logs = cls.gateway.log.read()
        cls.gateway.close()
        cls.backend.shutdown()
        cls.backend.server_close()
        if cls.gateway.process.returncode != 0 or errors:
            raise AssertionError(f"Gateway did not exit cleanly: {cls.gateway.process.returncode}; {errors}")
        if API_KEY in logs or "private-question-marker" in logs or "secret-query-marker" in logs:
            raise AssertionError("Private input leaked into access logs")
        rows = [json.loads(line) for line in logs.splitlines()]
        if not any(row.get("reason") == "timeout" for row in rows):
            raise AssertionError("Timeout diagnostics missing")

    def test_01_health_and_readiness(self):
        for path in ("/livez", "/readyz"):
            self.assertEqual(self.gateway.request("GET", path, auth=False)[0], 200)

    def test_02_authentication_is_required(self):
        for headers in ({}, {"X-API-Key": "wrong"}):
            self.assertEqual(self.gateway.request("GET", "/api/documents", auth=False, headers=headers)[0], 401)

    def test_03_query_and_headers_are_forwarded_safely(self):
        target = "/api/documents?collection_id=root&marker=secret-query-marker&text=%E4%B8%AD%E6%96%87"
        status, headers, body = self.gateway.request("GET", target, headers={
            "Connection": "X-Client-Hop", "X-Client-Hop": "remove-me", "Authorization": "private",
            "X-Forwarded-For": "spoofed", "X-User-ID": "spoofed", "X-Request-ID": "spoofed",
        })
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["path"], target)
        upstream_headers = {key.lower(): value for key, value in result["headers"].items()}
        for key in ("x-api-key", "authorization", "x-forwarded-for", "x-user-id", "x-client-hop"):
            self.assertNotIn(key, upstream_headers)
        self.assertEqual(upstream_headers["x-request-id"], dict(headers)["X-Request-ID"])
        self.assertNotEqual(upstream_headers["x-request-id"], "spoofed")
        self.assertEqual(upstream_headers["accept-encoding"], "identity")

    def test_04_post_keeps_body_content_type_and_status(self):
        body = json.dumps({"question": "private-question-marker 中文", "request_id": "demo-001"}).encode()
        status, headers, received = self.gateway.request("POST", "/api/conversations/chat-1/turns", body,
            headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 201)
        self.assertEqual(body, received)
        self.assertEqual(dict(headers)["X-Upstream"], "fake-agent")
        self.assertEqual(self.backend.requests[-1][3], body)

    def test_05_business_failure_preserves_http_200(self):
        status, _, body = self.gateway.request("POST", "/api/conversations/chat/turns?case=failed", b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "failed")

    def test_06_routes_and_methods_are_explicit(self):
        before = len(self.backend.requests)
        for method, target in (("DELETE", "/api/documents/x"), ("GET", "/api/not-enabled"),
                               ("GET", "/api/conversations/x/turns/stream"), ("GET", "/openapi.json"),
                               ("GET", "/api/documents/")):
            self.assertEqual(self.gateway.request(method, target)[0], 404)
        self.assertEqual(len(self.backend.requests), before)

    def test_07_upstream_errors_are_not_rewritten(self):
        status, _, body = self.gateway.request("GET", "/api/documents?case=status")
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body), {"detail": "synthetic validation error"})

    def test_08_chunked_json_and_hop_headers(self):
        status, headers, body = self.gateway.request("GET", "/api/documents?case=chunked")
        lowered = [(name.lower(), value) for name, value in headers]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"text": "中文"})
        self.assertNotIn("x-backend-hop", dict(lowered))
        self.assertNotIn("transfer-encoding", dict(lowered))
        self.assertEqual(dict(lowered)["content-length"], str(len(body)))
        self.assertEqual(sum(name == "set-cookie" for name, _ in lowered), 2)

    def test_09_abrupt_upstream_is_502(self):
        self.assertEqual(self.gateway.request("GET", "/api/documents?case=abrupt")[0], 502)

    def test_10_deadline_and_recovery(self):
        start = time.monotonic()
        self.assertEqual(self.gateway.request("GET", "/api/documents?case=slow")[0], 504)
        self.assertLess(time.monotonic() - start, 1.1)
        self.assertEqual(self.gateway.request("GET", "/api/documents")[0], 200)

    def test_11_json_route_does_not_silently_change_to_sse(self):
        status, _, body = self.gateway.request("POST", "/api/ask?case=stream", b"{}")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["code"], "UNEXPECTED_STREAM")

    def test_12_upstream_body_limit(self):
        self.assertEqual(self.gateway.request("GET", "/api/documents?case=huge")[0], 502)

    def test_13_request_limits_and_duplicate_keys(self):
        for extra, expected in (
            ("Content-Length: 1048577\r\n", 413),
            (f"X-API-Key: {API_KEY}\r\n", 401),
            ("Expect: 100-continue\r\nContent-Length: 2\r\n", 417),
        ):
            with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
                connection.sendall((f"POST /api/ask HTTP/1.1\r\nHost: local\r\nX-API-Key: {API_KEY}\r\n" + extra + "\r\n").encode())
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, expected)
                response.read()

    def test_14_readiness_can_fail_without_liveness_failing(self):
        self.backend.health_status = 503
        try:
            self.assertEqual(self.gateway.request("GET", "/readyz", auth=False)[0], 503)
            self.assertEqual(self.gateway.request("GET", "/livez", auth=False)[0], 200)
        finally:
            self.backend.health_status = 200

    def test_15_unavailable_backend(self):
        other = GatewayProcess(available_port())
        try:
            self.assertEqual(other.request("GET", "/api/documents")[0], 502)
            self.assertEqual(other.request("GET", "/readyz", auth=False)[0], 503)
            self.assertEqual(other.request("GET", "/livez", auth=False)[0], 200)
        finally:
            other.close()

    def test_16_slow_model_does_not_block_health(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            slow = executor.submit(self.gateway.request, "POST", "/api/ask?case=slow", b"{}")
            time.sleep(0.08)
            start = time.monotonic()
            self.assertEqual(self.gateway.request("GET", "/livez", auth=False)[0], 200)
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertEqual(slow.result()[0], 504)

    def test_17_slow_request_headers_time_out(self):
        with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
            connection.sendall(b"GET /api/documents HTTP/1.1\r\nHost:")
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 408)
            response.read()

    def test_18_connections_are_reclaimed_after_disconnects(self):
        fd_dir = Path(f"/proc/{self.gateway.process.pid}/fd")
        baseline = len(list(fd_dir.iterdir()))
        for _ in range(100):
            with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
                connection.sendall(f"GET /api/documents HTTP/1.1\r\nHost: local\r\nX-API-Key: {API_KEY}\r\n\r\n".encode())
        deadline = time.monotonic() + 3
        stable_since = None
        while time.monotonic() < deadline:
            count = len(list(fd_dir.iterdir()))
            if count <= baseline + 2:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 0.2:
                    break
            else:
                stable_since = None
            time.sleep(0.03)
        self.assertLessEqual(len(list(fd_dir.iterdir())), baseline + 2)
        self.assertEqual(self.gateway.request("GET", "/api/documents")[0], 200)

    def test_19_invalid_configuration_fails_without_exposing_values(self):
        for settings in ({}, {"GATEWAY_API_KEY": API_KEY, "GATEWAY_UPSTREAM": "http://private-user:secret@host:80"}):
            result = subprocess.run([BINARY], env={"PATH": os.environ["PATH"], **settings}, capture_output=True, timeout=4)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(API_KEY.encode(), result.stderr)
            self.assertNotIn(b"private-user", result.stderr)

    def test_20_shutdown_with_pending_work(self):
        other = GatewayProcess(self.backend.server_port)
        try:
            connection = socket.create_connection(("127.0.0.1", other.port), timeout=3)
            connection.sendall(f"POST /api/ask?case=slow HTTP/1.1\r\nHost: local\r\nX-API-Key: {API_KEY}\r\nContent-Length: 2\r\n\r\n{{}}".encode())
            time.sleep(0.05)
            other.stop()
            connection.close()
            self.assertEqual(other.process.returncode, 0)
        finally:
            other.close()

    def test_21_failed_post_is_not_automatically_retried(self):
        target = "/api/ask?case=abrupt&marker=no-retry-probe"
        self.assertEqual(self.gateway.request("POST", target, b"{}")[0], 502)
        self.assertEqual(sum(method == "POST" and path == target for method, path, _, _ in self.backend.requests), 1)

    def test_22_chunked_request_is_reframed(self):
        target = "/api/ask?marker=chunked-request"
        status, _, body = self.gateway.request("POST", target, iter([b'{"text":"', "中文".encode(), b'"}']),
                                               headers={"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body), {"text": "中文"})
        upstream = next(row for row in self.backend.requests if row[0] == "POST" and row[1] == target)
        received = {name.lower(): value for name, value in upstream[2].items()}
        self.assertNotIn("transfer-encoding", received)
        self.assertEqual(int(received["content-length"]), len(body))

    def test_23_linux_event_loop_uses_epoll(self):
        targets = []
        for path in Path(f"/proc/{self.gateway.process.pid}/fd").iterdir():
            try:
                targets.append(os.readlink(path))
            except FileNotFoundError:
                pass
        self.assertIn("anon_inode:[eventpoll]", targets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    args, remaining = parser.parse_known_args()
    BINARY = str(Path(args.binary).resolve(strict=True))
    unittest.main(argv=[__file__, *remaining], verbosity=2)
