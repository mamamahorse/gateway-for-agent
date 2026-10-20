"""Timing, framing, backpressure and upload tests of the actual C++ proxy."""

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import socket
import threading
import time
import unittest

from fake_agent import make_server
import test_proxy


STREAM = "/api/conversations/public-chat/turns/stream"
OBSERVATIONS = {}


def rss_kib(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError("VmRSS not available")


def wait_for_fds(pid, baseline):
    deadline = time.monotonic() + 4
    stable = None
    while time.monotonic() < deadline:
        count = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        if count <= baseline + 2:
            stable = stable or time.monotonic()
            if time.monotonic() - stable >= 0.2:
                return count
        else:
            stable = None
        time.sleep(0.03)
    return count


class StreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = make_server()
        cls.thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.thread.start()
        cls.settings = {
            "GATEWAY_HEADER_TIMEOUT_MS": "1000",
            "GATEWAY_STREAM_TIMEOUT_MS": "2500",
            "GATEWAY_STREAM_FIRST_BYTE_TIMEOUT_MS": "500",
            "GATEWAY_STREAM_IDLE_TIMEOUT_MS": "500",
            "GATEWAY_STREAM_WRITE_TIMEOUT_MS": "350",
            "GATEWAY_UPLOAD_TIMEOUT_MS": "5000",
        }
        cls.gateway = test_proxy.GatewayProcess(cls.backend.server_port, settings=cls.settings)

    @classmethod
    def tearDownClass(cls):
        cls.gateway.stop()
        cls.gateway.errors.seek(0)
        errors = cls.gateway.errors.read()
        cls.gateway.log.seek(0)
        rows = [json.loads(line) for line in cls.gateway.log.read().splitlines()]
        cls.gateway.close()
        cls.backend.shutdown()
        cls.backend.server_close()
        if errors or cls.gateway.process.returncode != 0:
            raise AssertionError(f"Gateway did not shut down cleanly: {errors}")
        reasons = {row.get("reason") for row in rows}
        if not {"stream_complete", "upstream_stream_error", "stream_read_timeout", "stream_write_error"} <= reasons:
            raise AssertionError(f"Missing stream terminal diagnostics: {reasons}")

    def open_stream(self, case="events", gateway=None):
        gateway = gateway or self.gateway
        connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=5)
        connection.request("POST", STREAM + "?case=" + case, b"{}", {
            "X-API-Key": test_proxy.API_KEY, "Content-Type": "application/json"})
        return connection, connection.getresponse()

    def test_01_events_arrive_before_upstream_finishes(self):
        start = time.monotonic()
        connection, response = self.open_stream()
        try:
            self.assertEqual(response.status, 200)
            lines = []
            frame = []
            arrivals = []
            while line := response.readline():
                lines.append(line)
                frame.append(line)
                if line in (b"\n", b"\r\n"):
                    if b"event: progress" in b"".join(frame):
                        arrivals.append(time.monotonic() - start)
                    frame = []
            total = time.monotonic() - start
            self.assertEqual(len(arrivals), 3)
            self.assertLess(arrivals[0], 0.35)
            self.assertLess(arrivals[1], total - 0.3)
            self.assertLess(arrivals[2], total - 0.12)
            self.assertGreater(total, 0.65)
            body = b"".join(lines)
            self.assertEqual(body.count(b"event: progress"), 3)
            self.assertEqual(body.count(b"event: done"), 1)
            self.assertLess(body.index(b'"seq":0'), body.index(b'"seq":2'))
            self.assertNotIn("X-Stream-Hop", dict(response.getheaders()))
            self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
            OBSERVATIONS["event_timing_ms"] = {"progress_events": [round(t * 1000) for t in arrivals], "complete": round(total * 1000)}
        finally:
            connection.close()

    def test_02_utf8_and_sse_boundaries_are_opaque_bytes(self):
        connection, response = self.open_stream("utf8")
        try:
            expected = b'data: {"text":"' + "中文".encode() + b'"}\n\nevent: done\ndata: {}\n\n'
            self.assertEqual(response.read(), expected)
        finally:
            connection.close()

    def test_03_json_success_on_stream_route_is_rejected(self):
        status, _, body = self.gateway.request("POST", STREAM + "?case=wrong_type", b"{}")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_STREAM_RESPONSE")

    def test_04_upstream_http_error_is_not_converted_to_sse(self):
        for case in ("status", "status_event"):
            with self.subTest(case=case):
                status, headers, body = self.gateway.request("POST", STREAM + "?case=" + case, b"{}")
                self.assertEqual(status, 429)
                self.assertEqual(dict(headers)["Retry-After"], "2")
                if case == "status":
                    self.assertIn("detail", json.loads(body))
                else:
                    self.assertEqual(body, b'event: error\ndata: {"code":"LIMITED"}\n\n')

    def test_05_incomplete_upstream_is_not_marked_complete(self):
        connection, response = self.open_stream("abrupt_stream")
        try:
            with self.assertRaises(http.client.IncompleteRead) as error:
                response.read()
            self.assertIn(b"event: progress", error.exception.partial)
            self.assertNotIn(b"event: done", error.exception.partial)
        finally:
            connection.close()

    def test_06_first_byte_deadline(self):
        start = time.monotonic()
        connection, response = self.open_stream("first_delay")
        try:
            self.assertEqual(response.status, 200) # Header already committed: cannot replace it with 504.
            with self.assertRaises(http.client.IncompleteRead):
                response.read()
            self.assertLess(time.monotonic() - start, 0.9)
        finally:
            connection.close()

    def test_07_stream_idle_deadline(self):
        connection, response = self.open_stream("stall")
        try:
            with self.assertRaises(http.client.IncompleteRead) as error:
                response.read()
            self.assertIn(b"progress", error.exception.partial)
        finally:
            connection.close()

    def test_08_heartbeats_do_not_extend_total_deadline(self):
        other = test_proxy.GatewayProcess(self.backend.server_port,
            settings={**self.settings, "GATEWAY_STREAM_TIMEOUT_MS": "700"})
        try:
            start = time.monotonic()
            connection, response = self.open_stream("heartbeat", other)
            try:
                with self.assertRaises(http.client.IncompleteRead) as error:
                    response.read()
                self.assertIn(b": ping", error.exception.partial)
                self.assertLess(time.monotonic() - start, 1.1)
            finally:
                connection.close()
        finally:
            other.close()

    def test_09_slow_reader_keeps_memory_bounded_and_times_out(self):
        baseline_rss = rss_kib(self.gateway.process.pid)
        baseline_fds = len(list(Path(f"/proc/{self.gateway.process.pid}/fd").iterdir()))
        connection = socket.socket()
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        connection.settimeout(4)
        connection.connect(("127.0.0.1", self.gateway.port))
        connection.sendall(f"POST {STREAM}?case=flood HTTP/1.1\r\nHost: local\r\nX-API-Key: {test_proxy.API_KEY}\r\nContent-Length: 2\r\n\r\n{{}}".encode())
        response = http.client.HTTPResponse(connection)
        response.begin()
        try:
            self.assertEqual(response.status, 200)
            time.sleep(0.2)
            growth = rss_kib(self.gateway.process.pid) - baseline_rss
            self.assertLess(growth, 12 * 1024)
            start = time.monotonic()
            self.assertEqual(self.gateway.request("GET", "/livez", auth=False)[0], 200)
            self.assertLess(time.monotonic() - start, 0.3)
            time.sleep(0.7)
            with self.assertRaises(http.client.IncompleteRead):
                response.read()
            OBSERVATIONS["slow_reader_rss_growth_kib"] = growth
        finally:
            response.close()
            connection.close()
        self.assertLessEqual(wait_for_fds(self.gateway.process.pid, baseline_fds), baseline_fds + 2)

    def test_10_stream_disconnects_reclaim_connections(self):
        pid = self.gateway.process.pid
        baseline = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        threads_before = len(list(Path(f"/proc/{pid}/task").iterdir()))
        for _ in range(100):
            connection, response = self.open_stream()
            response.read(1)
            response.close()
            connection.close()
        final_fds = wait_for_fds(pid, baseline)
        self.assertLessEqual(final_fds, baseline + 2)
        threads_after = len(list(Path(f"/proc/{pid}/task").iterdir()))
        self.assertLessEqual(threads_after, threads_before)
        OBSERVATIONS["stream_disconnects"] = {"count": 100, "fds_before": baseline, "fds_after": final_fds,
                                               "threads_before": threads_before, "threads_after": threads_after}

    def test_11_upload_larger_than_json_limit_preserves_bytes(self):
        body = b"--public-boundary\r\n" + b"x" * (3 * 1024 * 1024) + b"\r\n--public-boundary--\r\n"
        content_type = "multipart/form-data; boundary=public-boundary"
        status, _, data = self.gateway.request("POST", "/api/documents?collection_id=root", body,
            headers={"Content-Type": content_type})
        result = json.loads(data)
        self.assertEqual(status, 201)
        self.assertEqual(result["bytes"], len(body))
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(result["content_type"], content_type)
        self.assertEqual(result["transfer_encoding"], "chunked")

    def test_12_upload_starts_before_client_finishes_sending(self):
        target = "/api/documents?marker=early-forward"
        body = b"x" * (2 * 1024 * 1024)
        with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=4) as connection:
            connection.sendall((f"POST {target} HTTP/1.1\r\nHost: local\r\nX-API-Key: {test_proxy.API_KEY}\r\n"
                f"Content-Type: multipart/form-data; boundary=public\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body[:16384])
            deadline = time.monotonic() + 0.5
            while self.backend.upload_progress.get(target, 0) == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(self.backend.upload_progress.get(target, 0), 0)
            connection.sendall(body[16384:])
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 201)
            self.assertEqual(json.loads(response.read())["sha256"], hashlib.sha256(body).hexdigest())

    def test_13_chunked_client_upload(self):
        blocks = [b"prefix\r\n", b"x" * 16384, "中文".encode(), b"\r\nsuffix"]
        status, _, data = self.gateway.request("POST", "/api/documents", iter(blocks),
            headers={"Content-Type": "Multipart/Form-Data ; boundary=public"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(data)["sha256"], hashlib.sha256(b"".join(blocks)).hexdigest())
        self.assertEqual(json.loads(data)["content_type"], "Multipart/Form-Data ; boundary=public")

    def test_14_unauthenticated_upload_gets_no_continue(self):
        with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
            connection.sendall(b"POST /api/documents HTTP/1.1\r\nHost: local\r\nContent-Type: multipart/form-data; boundary=public\r\nExpect: 100-continue\r\nContent-Length: 2\r\n\r\n")
            header = connection.recv(4096)
            self.assertTrue(header.startswith(b"HTTP/1.1 401"), header)

    def test_15_upload_continue_handshake(self):
        body = b"public-upload"
        with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
            connection.sendall((f"POST /api/documents HTTP/1.1\r\nHost: local\r\nX-API-Key: {test_proxy.API_KEY}\r\n"
                f"Content-Type: multipart/form-data; boundary=public\r\nExpect: 100-continue\r\nContent-Length: {len(body)}\r\n\r\n").encode())
            header = b""
            while b"\r\n\r\n" not in header:
                header += connection.recv(4096)
            self.assertTrue(header.startswith(b"HTTP/1.1 100 Continue"), header)
            connection.sendall(body)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 201)
            self.assertEqual(json.loads(response.read())["bytes"], len(body))

    def test_16_known_oversized_upload_is_rejected_before_body(self):
        with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
            connection.sendall((f"POST /api/documents HTTP/1.1\r\nHost: local\r\nX-API-Key: {test_proxy.API_KEY}\r\n"
                "Content-Type: multipart/form-data; boundary=public\r\nContent-Length: 436207617\r\n\r\n").encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 413)
            response.read()

    def test_17_upload_requires_multipart(self):
        self.assertEqual(self.gateway.request("POST", "/api/documents", b"{}", headers={"Content-Type": "application/json"})[0], 415)

    def test_18_early_upstream_rejection_does_not_wait_for_upload(self):
        with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
            connection.sendall((f"POST /api/documents?case=reject_upload HTTP/1.1\r\nHost: local\r\nX-API-Key: {test_proxy.API_KEY}\r\n"
                "Content-Type: multipart/form-data; boundary=public\r\nContent-Length: 2097152\r\n\r\n").encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 413)
            self.assertIn(b"synthetic early", response.read())

    def test_19_encoded_sse_is_rejected_before_commit(self):
        status, _, data = self.gateway.request("POST", STREAM + "?case=encoded", b"{}")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(data)["error"]["code"], "ENCODED_STREAM_UNSUPPORTED")

    def test_20_large_upload_keeps_gateway_memory_bounded(self):
        # Instrumented builds can take longer to move 32 MiB; keep timeout
        # behavior in test_21 and give this memory/byte-integrity probe its own budget.
        other = test_proxy.GatewayProcess(self.backend.server_port,
            settings={**self.settings, "GATEWAY_UPLOAD_TIMEOUT_MS": "30000", "GATEWAY_HEADER_TIMEOUT_MS": "3000"})
        try:
            self.check_large_upload(other)
        finally:
            other.close()

    def check_large_upload(self, gateway):
        target = "/api/documents?marker=large-upload-memory"
        block = b"x" * 65536
        digest = hashlib.sha256()
        for _ in range(512):
            digest.update(block)
        baseline = rss_kib(gateway.process.pid)
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", gateway.port), timeout=15) as connection:
            connection.sendall((f"POST {target} HTTP/1.1\r\nHost: local\r\nX-API-Key: {test_proxy.API_KEY}\r\n"
                "Content-Type: multipart/form-data; boundary=public\r\nContent-Length: 33554432\r\n\r\n").encode())
            for _ in range(256):
                connection.sendall(block)
            deadline = time.monotonic() + 0.5
            while self.backend.upload_progress.get(target, 0) < 1024 * 1024 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(self.backend.upload_progress.get(target, 0), 1024 * 1024)
            growth = rss_kib(gateway.process.pid) - baseline
            self.assertLess(growth, 12 * 1024)
            for _ in range(256):
                connection.sendall(block)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 201)
            self.assertEqual(json.loads(response.read())["sha256"], digest.hexdigest())
            OBSERVATIONS["upload_memory"] = {"payload_bytes": 33554432, "mid_upload_rss_growth_kib": growth,
                                              "duration_ms": round((time.monotonic() - started) * 1000)}

    def test_21_stalled_upload_times_out_as_client_error(self):
        with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
            connection.sendall((f"POST /api/documents HTTP/1.1\r\nHost: local\r\nX-API-Key: {test_proxy.API_KEY}\r\n"
                "Content-Type: multipart/form-data; boundary=public\r\nContent-Length: 2097152\r\n\r\nx").encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 408)
            self.assertEqual(json.loads(response.read())["error"]["code"], "CLIENT_TIMEOUT")

    def test_22_ordinary_request_half_close_keeps_response_readable(self):
        with socket.create_connection(("127.0.0.1", self.gateway.port), timeout=3) as connection:
            connection.sendall((f"GET /api/documents HTTP/1.1\r\nHost: local\r\nX-API-Key: {test_proxy.API_KEY}\r\n\r\n").encode())
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 200)
            self.assertTrue(response.read())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    args, remaining = parser.parse_known_args()
    test_proxy.BINARY = str(Path(args.binary).resolve(strict=True))
    program = unittest.main(argv=[__file__, *remaining], verbosity=2, exit=False)
    print("TRANSPORT_OBSERVATIONS=" + json.dumps(OBSERVATIONS), flush=True)
    raise SystemExit(not program.result.wasSuccessful())
