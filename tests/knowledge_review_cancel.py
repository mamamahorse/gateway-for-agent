"""Run cancellation_smoke against an isolated public Agent and controlled model."""
import argparse
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from cancel_model import CancelModelHandler, initialize
from cancellation_smoke import run as verify
from knowledge_review_sse import stop
from test_rate_limit import RedisServer
import test_proxy


def run(project, binary):
    runtime = [p for p in project.glob('*.py') if not p.name.startswith(('test_', 'quality_'))]
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime}
    model = initialize(ThreadingHTTPServer(('127.0.0.1', 0), CancelModelHandler))
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    gateway = backend = redis = None
    try:
        with tempfile.TemporaryDirectory(prefix='kr-cancel-') as folder, tempfile.TemporaryFile() as logs:
            isolated = Path(folder)
            for path in runtime:
                shutil.copyfile(path, isolated / path.name)
            shutil.copytree(project / 'web', isolated / 'web')
            shutil.copytree(project / 'examples', isolated / 'examples')
            port = test_proxy.available_port()
            env = {'PATH': os.environ['PATH'], 'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'LLM_API_KEY': 'public-local-test', 'LLM_MODEL': 'controlled',
                   'LLM_BASE_URL': f'http://127.0.0.1:{model.server_port}/v1',
                   'KNOWLEDGE_REVIEW_MODEL_WORKERS': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
            backend = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'web_app:app', '--host', '127.0.0.1',
                                        '--port', str(port)], cwd=isolated, env=env, stdout=logs, stderr=logs)
            redis = RedisServer()
            gateway = test_proxy.GatewayProcess(port, settings={
                'GATEWAY_HEADER_TIMEOUT_MS': '2000', 'GATEWAY_MODEL_TIMEOUT_MS': '1500',
                'GATEWAY_STREAM_TIMEOUT_MS': '4000', 'GATEWAY_STREAM_IDLE_TIMEOUT_MS': '700',
                'GATEWAY_STREAM_FIRST_BYTE_TIMEOUT_MS': '1500', 'GATEWAY_ADMISSION_ENABLED': '1',
                'GATEWAY_MODEL_CONCURRENCY': '1', 'GATEWAY_RATE_LIMIT_ENABLED': '1',
                'GATEWAY_REDIS_PORT': str(redis.port), 'GATEWAY_REDIS_TIMEOUT_MS': '500',
                'GATEWAY_RATE_LIMIT_PREFIX': 'test:m232:cancel',
                'GATEWAY_RATE_LIMIT_NORMAL': '1000', 'GATEWAY_RATE_LIMIT_MODEL': '1000'})
            result = verify(gateway.port, test_proxy.API_KEY, model.server_port)
            time.sleep(.2)
            result['backend_alive_after_checks'] = backend.poll() is None
    except BaseException:
        if not logs.closed:
            logs.seek(0)
            print(logs.read().decode('utf-8', errors='replace')[-8000:], file=sys.stderr)
        raise
    finally:
        if gateway is not None:
            gateway.stop()
            gateway.errors.seek(0)
            errors = gateway.errors.read()
            gateway.close()
            if errors:
                raise AssertionError(errors)
        if backend is not None:
            stop(backend)
        if redis is not None:
            redis.close()
        model.shutdown()
        model.server_close()
        thread.join(3)
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime}
    result.update(source_unchanged=True, runtime_source_files=len(runtime))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    test_proxy.BINARY = str(args.binary.resolve(strict=True))
    result = run(args.project.resolve(strict=True), args.binary.resolve(strict=True))
    args.report.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'checks_passed': result['checks_passed'], 'source_unchanged': True}))
