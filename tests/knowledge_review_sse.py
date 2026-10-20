"""Actual Gateway -> Uvicorn -> Agent -> controlled local OpenAI HTTP service.

No credentials, private documents, model caches or application data are copied.
This verifies the real orchestration and persistence, not model answer quality.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import test_proxy


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.requests.append(body)
        messages = str(body["messages"])
        if "hold-public" in messages:
            self.server.release.wait(12)
        time.sleep(0.12)
        if "fail-public" in messages:
            status, data = 400, {"error": {"message": "synthetic-provider-secret", "type": "invalid_request_error"}}
        else:
            status = 200
            if "tools" in body and not any(message["role"] == "tool" for message in body["messages"]):
                source_id = re.search(r"source_id=([^\s|]+)", messages).group(1)
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "public-read", "type": "function",
                    "function": {"name": "read_source", "arguments": json.dumps({"source_id": source_id, "line_start": 1, "line_end": 3})}}]}
                finish = "tool_calls"
            elif "tools" in body:
                message, finish = {"role": "assistant", "content": "ready"}, "stop"
            else:
                answer = {"coverage": "none", "coverage_note": "Controlled transport verification", "blocks": [
                    {"kind": "supplement", "text": "Public controlled answer", "citations": []}]}
                message, finish = {"role": "assistant", "content": json.dumps(answer)}, "stop"
            data = {"id": "public", "object": "chat.completion", "created": 0, "model": "controlled",
                    "choices": [{"index": 0, "finish_reason": finish, "message": message}]}
        encoded = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def read_events(response, start):
    result = []
    event = None
    while line := response.readline():
        line = line.decode("utf-8").rstrip("\r\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            result.append({"event": event, "data": json.loads(line[6:]), "ms": round((time.monotonic() - start) * 1000)})
    return result


def run(project, binary, redis_port=None):
    files = [p for p in project.glob("*.py") if not p.name.startswith(("test_", "quality_"))]
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    model.requests, model.lock, model.release = [], threading.Lock(), threading.Event()
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    checks = []
    gateway = backend = None
    observations = {}
    try:
        with tempfile.TemporaryDirectory(prefix="kr-sse-") as folder, tempfile.TemporaryFile(mode="w+", encoding="utf-8") as logs:
            isolated = Path(folder)
            for path in files:
                shutil.copy2(path, isolated / path.name)
            shutil.copytree(project / "web", isolated / "web")
            (isolated / "examples").mkdir()
            for path in (project / "examples").glob("*.md"):
                shutil.copy2(path, isolated / "examples" / path.name)
            port = test_proxy.available_port()
            environment = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                           "LLM_API_KEY": "public-local-test", "LLM_MODEL": "controlled",
                           "LLM_BASE_URL": f"http://127.0.0.1:{model.server_port}/v1"}
            backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "web_app:app", "--host", "127.0.0.1", "--port", str(port)],
                                       cwd=isolated, env=environment, stdout=logs, stderr=logs)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if backend.poll() is not None:
                    logs.seek(0)
                    raise RuntimeError(logs.read())
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    conn.request("GET", "/healthz")
                    response = conn.getresponse()
                    response.read()
                    conn.close()
                    if response.status == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            else:
                raise RuntimeError("Backend did not start")
            test_proxy.BINARY = str(binary)
            settings = {"GATEWAY_STREAM_TIMEOUT_MS": "15000", "GATEWAY_MODEL_TIMEOUT_MS": "15000"}
            if redis_port is not None:
                settings.update({"GATEWAY_RATE_LIMIT_ENABLED":"1", "GATEWAY_REDIS_PORT":str(redis_port),
                                 "GATEWAY_REDIS_TIMEOUT_MS":"500", "GATEWAY_RATE_LIMIT_PREFIX":"test:real-agent",
                                 "GATEWAY_RATE_LIMIT_NORMAL":"100", "GATEWAY_RATE_LIMIT_MODEL":"100"})
            gateway = test_proxy.GatewayProcess(port, settings=settings)
            assert gateway.request("GET", "/readyz", auth=False)[0] == 200
            checks.append("real healthz through readiness without model calls")

            def new_payload(question="Explain public source", request_id="public-sse"):
                status, _, data = gateway.request("POST", "/api/conversations", b'{"title":"Public SSE"}', headers={"Content-Type": "application/json"})
                assert status == 201
                identity = json.loads(data)["id"]
                status, _, data = gateway.request("GET", "/api/documents/example-retrieval-demo")
                doc = json.loads(data)
                return identity, {"document_ids": [doc["id"]], "focus_document_id": doc["id"], "focus_source_id": doc["source_id"],
                                  "question": question, "request_id": request_id}

            def open_stream(identity, payload):
                connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=15)
                start = time.monotonic()
                connection.request("POST", f"/api/conversations/{identity}/turns/stream", json.dumps(payload),
                                   {"X-API-Key": test_proxy.API_KEY, "Content-Type": "application/json"})
                return connection, connection.getresponse(), start

            identity, payload = new_payload()
            connection, response, start = open_stream(identity, payload)
            assert response.status == 200
            received = read_events(response, start)
            connection.close()
            assert received[-1]["event"] == "done" and received[-1]["data"]["status"] == "completed", received
            progress = [r for r in received if r["event"] == "progress"]
            assert any(p["data"] == {"phase": "tool", "tool": "read_source", "status": "completed"} for p in progress)
            assert progress[0]["ms"] + 200 < received[-1]["ms"], received
            assert received[0]["data"]["trace_id"] == response.getheader("X-Request-ID")
            observations["events"] = [{"event": r["event"], "phase": r["data"].get("phase"), "ms": r["ms"]} for r in received]
            checks.append("incremental actual Agent read_source and final structured model HTTP flow")
            status, _, data = gateway.request("GET", f"/api/conversations/{identity}/messages")
            turns = json.loads(data)["turns"]
            final = next(r["data"]["turn"] for r in received if r["event"] == "result")
            assert turns == [final]
            checks.append("result event equals committed SQLite public turn")
            count = len(model.requests)
            connection, response, start = open_stream(identity, payload)
            replay = read_events(response, start)
            connection.close()
            assert replay[-1]["data"]["status"] == "completed" and len(model.requests) == count
            checks.append("completed request replay does not rerun tools or model")
            connection, response, _ = open_stream(identity, {**payload, "question": "changed"})
            assert response.status == 409 and b"event:" not in response.read()
            connection.close()
            checks.append("idempotency conflict remains HTTP 409 before SSE headers")

            failed_id, failed_payload = new_payload("fail-public")
            connection, response, start = open_stream(failed_id, failed_payload)
            failed_events = read_events(response, start)
            connection.close()
            assert failed_events[-1]["data"]["status"] == "failed"
            assert any(r["event"] == "error" for r in failed_events)
            assert "synthetic-provider-secret" not in json.dumps(failed_events)
            status, _, data = gateway.request("GET", f"/api/conversations/{failed_id}/messages")
            assert json.loads(data)["turns"][0]["status"] == "failed"
            checks.append("provider failure emits safe error/done and persists failed turn")

            # All four tasks block in a controlled upstream request. Dropping a
            # client must not prematurely free admission for a fifth task.
            held = []
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(open_stream, *new_payload("hold-public", f"hold-{i}")) for i in range(4)]
                for future in futures:
                    held.append(future.result(timeout=6))
            for _, response, _ in held:
                assert response.status == 200
            rejected_id, rejected_payload = new_payload("hold-public", "hold-extra")
            connection, response, _ = open_stream(rejected_id, rejected_payload)
            assert response.status == 503 and response.getheader("Retry-After") == "1"
            response.read()
            connection.close()
            checks.append("four real running stream tasks reject fifth with HTTP 503")
            held[0][1].close()
            held[0][0].close()
            time.sleep(0.15)
            connection, response, _ = open_stream(rejected_id, rejected_payload)
            assert response.status == 503
            response.read()
            connection.close()
            checks.append("client disconnect retains slot until synchronous work exits")
            assert gateway.request("GET", "/readyz", auth=False)[0] == 200
            checks.append("blocked model requests leave health checks responsive")
            model.release.set()
            for connection, response, start in held[1:]:
                done = read_events(response, start)
                assert done[-1]["data"]["status"] == "completed"
                connection.close()
            time.sleep(0.2)
            connection, response, start = open_stream(rejected_id, {**rejected_payload, "question": "after release"})
            assert response.status == 200
            assert read_events(response, start)[-1]["data"]["status"] == "completed"
            connection.close()
            checks.append("slots recover after actual work completion")
            observations["controlled_model_http_requests"] = len(model.requests)
            gateway.close()
            gateway = None
            stop(backend)
            backend = None
    finally:
        model.release.set()
        if gateway is not None:
            gateway.close()
        if backend is not None:
            stop(backend)
        model.shutdown()
        model.server_close()
        model_thread.join(timeout=3)
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return {"checks_passed": len(checks), "checks": checks, "observations": observations, "source_unchanged": True,
            "runtime_source_files": len(files), "external_model_calls": False, "model_quality_tested": False,
            "rate_limiting_enabled": redis_port is not None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--rate-limit", action="store_true", help="Run a disposable real Redis alongside the integration")
    args = parser.parse_args()
    redis = None
    try:
        if args.rate_limit:
            from test_rate_limit import RedisServer
            redis = RedisServer()
        result = run(args.project.resolve(strict=True), args.binary.resolve(strict=True), redis.port if redis else None)
    finally:
        if redis is not None:
            redis.close()
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
