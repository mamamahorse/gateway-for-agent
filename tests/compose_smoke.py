"""Validate synthetic business data through the published Compose gateway port."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import time

from knowledge_review_sse import read_events


def run(port, key, phase, state):
    checks = []
    def request(method, path, body=None, content_type="application/json", auth=True):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            headers = {"Content-Type": content_type}
            if auth:
                headers["X-API-Key"] = key
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def stream(identity, payload):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            start = time.monotonic()
            connection.request("POST", f"/api/conversations/{identity}/turns/stream", json.dumps(payload),
                               {"X-API-Key": key, "Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 200, (response.status, response.read())
            return read_events(response, start)
        finally:
            connection.close()

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if request("GET", "/readyz", auth=False)[0] == 200:
                break
        except OSError:
            pass
        time.sleep(0.25)
    else:
        raise RuntimeError("Compose gateway readiness timeout")
    checks.append("container gateway readiness against agent healthz")
    assert request("GET", "/api/documents", auth=False)[0] == 401
    checks.append("published gateway enforces API key")

    if phase == "create":
        boundary = "public-compose-boundary"
        markdown = "# Public Compose\n\nA synthetic document for persistence verification.\n"
        upload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="public-compose.md"\r\n'
                  f'Content-Type: text/markdown\r\n\r\n{markdown}\r\n--{boundary}--\r\n').encode()
        status, body = request("POST", "/api/documents", upload, f"multipart/form-data; boundary={boundary}")
        assert status == 201, (status, body)
        doc_id = json.loads(body)[0]["id"]
        status, body = request("GET", f"/api/documents/{doc_id}")
        document = json.loads(body)
        assert document["lines"] == markdown.splitlines()
        checks.append("multipart Markdown import persisted in container data volume")
        status, body = request("POST", "/api/conversations", b'{"title":"Public Compose SSE"}')
        assert status == 201
        identity = json.loads(body)["id"]
        payload = {"document_ids": [doc_id], "focus_document_id": doc_id, "focus_source_id": document["source_id"],
                   "question": "Explain public source", "request_id": "compose-public"}
        received = stream(identity, payload)
        assert received[-1]["data"]["status"] == "completed", received
        assert any(e["event"] == "progress" and e["data"].get("tool") == "read_source" and e["data"]["status"] == "completed" for e in received)
        checks.append("container Agent executes read_source and returns committed SSE result")
        final = next(e["data"]["turn"] for e in received if e["event"] == "result")
        assert received[0]["ms"] + 100 < received[-1]["ms"]
        checks.append("container SSE progress arrives before final result")
        state = {"conversation_id": identity, "document_id": doc_id, "payload": payload,
                 "turn_sha256": hashlib.sha256(json.dumps(final, sort_keys=True).encode()).hexdigest(),
                 "events": [{"event": e["event"], "ms": e["ms"]} for e in received]}
    else:
        identity = state["conversation_id"]
        status, body = request("GET", f"/api/documents/{state['document_id']}")
        assert status == 200 and json.loads(body)["source_id"] == state["payload"]["focus_source_id"]
        checks.append("document source identity preserved after container restart")
        received = stream(identity, state["payload"])
        assert [e["event"] for e in received] == ["meta", "result", "done"]
        checks.append("saved completed turn replays after container restart")
    status, body = request("GET", f"/api/conversations/{state['conversation_id']}/messages")
    turns = json.loads(body)["turns"]
    assert status == 200 and len(turns) == 1
    assert hashlib.sha256(json.dumps(turns[0], sort_keys=True).encode()).hexdigest() == state["turn_sha256"]
    checks.append("SQLite public turn equals saved result without duplicate rows")
    return {"phase": phase, "checks_passed": len(checks), "checks": checks, "state": state,
            "external_model_calls": False, "embedding_search_tested": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--key", required=True, help="Use a public synthetic key for this test stack")
    parser.add_argument("--phase", choices=["create", "restore"], required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.state.read_text(encoding="utf-8"))["state"] if args.state else None
    result = run(args.port, args.key, args.phase, state)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"phase": args.phase, "checks_passed": result["checks_passed"]}))
