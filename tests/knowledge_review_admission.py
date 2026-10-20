"""Real C++ -> Redis -> Uvicorn shared JSON/SSE workers, no external model."""
import argparse
from concurrent.futures import ThreadPoolExecutor
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

from knowledge_review_sse import ModelHandler, stop
from test_rate_limit import RedisServer
import test_proxy


def run(project, binary):
    runtime = [p for p in project.glob('*.py') if not p.name.startswith(('test_', 'quality_'))]
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime}
    model = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
    model.requests, model.lock, model.release = [], threading.Lock(), threading.Event()
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    checks, observations = [], {}
    gateway = backend = redis = None
    try:
        with tempfile.TemporaryDirectory(prefix='kr-m22-') as folder, tempfile.TemporaryFile() as backend_log:
            isolated = Path(folder)
            for path in runtime:
                shutil.copyfile(path, isolated / path.name)
            shutil.copytree(project / 'web', isolated / 'web')
            shutil.copytree(project / 'examples', isolated / 'examples')
            backend_port = test_proxy.available_port()
            env = {'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
                   'LLM_API_KEY': 'public-local-test', 'LLM_MODEL': 'controlled',
                   'LLM_BASE_URL': f'http://127.0.0.1:{model.server_port}/v1',
                   'KNOWLEDGE_REVIEW_MODEL_WORKERS': '2'}
            backend = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'web_app:app', '--host', '127.0.0.1',
                '--port', str(backend_port)], cwd=isolated, env=env, stdout=backend_log, stderr=backend_log)
            redis = RedisServer()
            gateway = test_proxy.GatewayProcess(backend_port, settings={
                'GATEWAY_HEADER_TIMEOUT_MS': '1000', 'GATEWAY_REQUEST_TIMEOUT_MS': '2000',
                'GATEWAY_MODEL_TIMEOUT_MS': '2500', 'GATEWAY_STREAM_TIMEOUT_MS': '5000',
                'GATEWAY_ADMISSION_ENABLED': '1', 'GATEWAY_MODEL_CONCURRENCY': '2',
                'GATEWAY_MODEL_QUEUE_LIMIT': '8', 'GATEWAY_MODEL_QUEUE_TIMEOUT_MS': '4000',
                'GATEWAY_RATE_LIMIT_ENABLED': '1', 'GATEWAY_REDIS_PORT': str(redis.port),
                'GATEWAY_REDIS_TIMEOUT_MS': '500', 'GATEWAY_RATE_LIMIT_PREFIX': 'test:m22:real',
                'GATEWAY_RATE_LIMIT_NORMAL': '100', 'GATEWAY_RATE_LIMIT_MODEL': '100'})
            end = time.monotonic() + 15
            while gateway.request('GET', '/readyz', auth=False)[0] != 200:
                if time.monotonic() >= end:
                    raise AssertionError('real Agent startup timed out')
                time.sleep(.05)
            checks.append('real Agent/Redis readiness')
            status, _, data = gateway.request('GET', '/api/documents/example-retrieval-demo')
            assert status == 200
            document = json.loads(data)

            def new_payload(question, request_id):
                status, _, data = gateway.request('POST', '/api/conversations', b'{"title":"Public M22"}',
                                                   headers={'Content-Type': 'application/json'})
                assert status == 201
                identity = json.loads(data)['id']
                payload = {'request_id': request_id, 'question': question, 'document_ids': [document['id']],
                           'focus_document_id': document['id'], 'focus_source_id': document['source_id']}
                return identity, payload

            def submit(identity, payload, stream=False):
                return gateway.request('POST', f'/api/conversations/{identity}/turns' + ('/stream' if stream else ''),
                    json.dumps(payload), headers={'Content-Type': 'application/json'})

            active = [new_payload('hold-public', f'public-active-{i}') for i in range(2)]
            queued = new_payload('Explain public source', 'public-queued')
            with ThreadPoolExecutor(max_workers=3) as pool:
                pending = [pool.submit(submit, *item) for item in active]
                end = time.monotonic() + 2
                while True:
                    with model.lock:
                        model_calls = len(model.requests)
                    if model_calls == 2:
                        break
                    assert time.monotonic() < end, ('model start timeout', model_calls)
                    time.sleep(.01)
                queued_future = pool.submit(submit, *queued, True)
                end = time.monotonic() + 1
                while b'"action":"queued"' not in os.pread(gateway.log.fileno(), 1024 * 1024, 0):
                    assert time.monotonic() < end, 'SSE did not enter shared gateway queue'
                    time.sleep(.01)
                assert gateway.request('GET', '/readyz', auth=False)[0] == 200
                assert gateway.request('GET', '/api/documents')[0] == 200
                checks.append('JSON fills two proxy slots; SSE queues while light traffic remains responsive')
                statuses = [future.result(5)[0] for future in pending]
                assert statuses == [504, 504], statuses
                checks.append('JSON proxy total deadlines release proxy permits')
                queued_status, _, queued_body = queued_future.result(5)
                assert queued_status == 503 and '模型执行槽位已满' in queued_body.decode(), queued_body
                with model.lock:
                    assert len(model.requests) == 2
                checks.append('queued SSE reaches full Python worker guard; timed-out JSON work retains real slots')
                status, _, data = submit(*queued)
                assert status == 503 and '模型执行槽位已满' in data.decode()
                checks.append('new JSON also rejected by the same Python worker guard')
                status, _, history = gateway.request('GET', f'/api/conversations/{queued[0]}/messages')
                assert status == 200 and json.loads(history)['turns'] == []
                checks.append('rejected JSON/SSE create no turn or model work')
                model.release.set()
                end = time.monotonic() + 8
                while True:
                    saved = [json.loads(gateway.request('GET', f'/api/conversations/{identity}/messages')[2])['turns']
                             for identity, _ in active]
                    if all(len(turns) == 1 and turns[0]['status'] == 'completed' for turns in saved):
                        break
                    assert time.monotonic() < end, 'actual synchronous work failed to complete'
                    time.sleep(.05)
                checks.append('timed-out HTTP requests can still commit once: cancellation remains deferred')
                # Completion callbacks run just after the commit; retry only admission rejection.
                end = time.monotonic() + 3
                while True:
                    status, _, data = submit(*queued, True)
                    if status != 503:
                        break
                    assert time.monotonic() < end
                    time.sleep(.02)
                assert status == 200 and b'event: result' in data and b'event: done' in data, (status, data)
                checks.append('actual completion returns shared capacity and a new SSE commits successfully')
                observations['blocked_model_calls_before_release'] = 2
                observations['sse_rejected_while_real_workers_busy'] = queued_status
                observations['saved_turns_after_http_timeouts'] = len(saved)
    finally:
        model.release.set()
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
        model_thread.join(3)
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime}
    return {'checks_passed': len(checks), 'checks': checks, 'observations': observations,
            'source_unchanged': True, 'external_model_calls': False, 'full_cancellation_implemented': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    test_proxy.BINARY = str(args.binary.resolve(strict=True))
    result = run(args.project.resolve(strict=True), args.binary.resolve(strict=True))
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
