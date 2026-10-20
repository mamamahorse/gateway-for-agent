"""Run actual Knowledge Review HTTP code in a disposable, data-free copy."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import test_proxy


def http_request(port, method, target, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def run(project, binary):
    files = [path for path in project.glob("*.py")
             if not path.name.startswith(("test_", "quality_"))]
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    checks = []
    with tempfile.TemporaryDirectory(prefix="ai-gateway-kr-smoke-") as folder:
        isolated = Path(folder)
        for path in files:
            shutil.copy2(path, isolated / path.name)
        shutil.copytree(project / "web", isolated / "web")
        (isolated / "examples").mkdir()
        for path in (project / "examples").glob("*.md"):
            shutil.copy2(path, isolated / "examples" / path.name)
        # No .env, .data, user notes, model caches, or Windows venv are copied.
        port = test_proxy.available_port()
        environment = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
            backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "web_app:app", "--host", "127.0.0.1",
                                        "--port", str(port)], cwd=isolated, env=environment,
                                       stdout=output, stderr=output)
            gateway = None
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if backend.poll() is not None:
                        output.seek(0)
                        raise RuntimeError("Isolated backend did not start: " + output.read())
                    try:
                        if http_request(port, "GET", "/api/documents")[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Isolated backend startup timed out")
                boundary = "public-gateway-smoke-boundary"
                test_proxy.BINARY = str(binary)
                gateway = test_proxy.GatewayProcess(port, health_path="/api/documents", settings={"GATEWAY_UPLOAD_TIMEOUT_MS": "10000"})
                markdown = "# Gateway smoke\n\nPublic synthetic document.\n"
                upload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="gateway-smoke.md"\r\n'
                          f'Content-Type: text/markdown\r\n\r\n{markdown}\r\n--{boundary}--\r\n').encode()
                status, _, data = gateway.request("POST", "/api/documents", upload,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                assert status == 201, (status, data)
                document_id = json.loads(data)[0]["id"]
                checks.append("multipart Markdown upload through streaming gateway")
                assert gateway.request("GET", "/readyz", auth=False)[0] == 200
                checks.append("readiness against actual document API")
                status, _, data = gateway.request("GET", "/api/documents")
                assert status == 200 and any(row["id"] == document_id for row in json.loads(data))
                checks.append("real document catalog includes isolated upload")
                status, _, data = gateway.request("GET", f"/api/documents/{document_id}")
                detail = json.loads(data)
                assert status == 200 and detail["lines"] == markdown.splitlines() and detail["source_id"]
                checks.append("real document lines and version identity preserved")
                status, _, data = gateway.request("POST", "/api/conversations", b'{"title":"Gateway smoke"}',
                    headers={"Content-Type": "application/json"})
                assert status == 201, (status, data)
                conversation_id = json.loads(data)["id"]
                checks.append("real conversation creation with SQLite")
                status, _, data = gateway.request("GET", f"/api/conversations/{conversation_id}/messages")
                assert status == 200 and json.loads(data)["turns"] == []
                checks.append("real empty conversation history")
                status, _, data = gateway.request("POST", "/api/ask", b"{}", headers={"Content-Type": "application/json"})
                assert status == 422 and "detail" in json.loads(data)
                checks.append("actual FastAPI validation error preserved")
                import pymupdf
                with pymupdf.open() as pdf:
                    pdf.new_page().insert_text((72, 72), "Public Gateway PDF smoke")
                    pdf_bytes = pdf.tobytes()
                pdf_upload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="public-gateway.pdf"\r\n'
                              'Content-Type: application/pdf\r\n\r\n').encode() + pdf_bytes + f'\r\n--{boundary}--\r\n'.encode()
                status, _, data = gateway.request("POST", "/api/documents", pdf_upload,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                assert status == 201, (status, data)
                pdf_id = json.loads(data)[0]["id"]
                checks.append("real multipart PDF import through gateway")
                status, _, data = gateway.request("GET", f"/api/documents/{pdf_id}")
                pdf_result = json.loads(data)
                assert status == 200 and pdf_result["format"] == "pdf" and pdf_result["page_count"] == 1
                checks.append("real PDF parser returns unified document metadata")
                status, _, data = gateway.request("POST", f"/api/conversations/{conversation_id}/turns/stream", b"{}",
                    headers={"Content-Type": "application/json"})
                assert status == 422 and "detail" in json.loads(data)
                checks.append("Python SSE endpoint validates the actual TurnRequest before streaming")
                assert (isolated / ".data" / "practice.sqlite3").is_file()
                assert (isolated / ".data" / "conversations.sqlite3").is_file()
                checks.append("all test business writes remain inside disposable copy")
            finally:
                if gateway:
                    gateway.close()
                backend.terminate()
                try:
                    backend.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    backend.kill()
                    backend.wait()
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    assert before == after, "Source changed during isolated smoke test"
    return {"checks": checks, "checks_passed": len(checks), "runtime_source_files": len(files),
            "source_unchanged": True, "model_calls": False,
            "scope": "Actual FastAPI/SQLite/document endpoints; no RAG or model semantics tested."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = run(args.project.resolve(strict=True), args.binary.resolve(strict=True))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
