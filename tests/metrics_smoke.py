"""Verify real Agent JSON/SSE business failures through a published gateway port."""
import argparse
import http.client
import json
from pathlib import Path
import time
from uuid import uuid4

from knowledge_review_sse import read_events


def run(port, key):
    checks, observations = [], {}

    def request(method, path, payload=None, auth=True):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        try:
            headers = {'Content-Type': 'application/json'}
            if auth:
                headers['X-API-Key'] = key
            connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def snapshot():
        status, body = request('GET', '/metrics')
        assert status == 200, (status, body)
        return json.loads(body)

    end = time.monotonic() + 20
    while True:
        try:
            if request('GET', '/readyz', auth=False)[0] == 200:
                break
        except OSError:
            pass
        assert time.monotonic() < end, 'Gateway readiness timeout'
        time.sleep(.1)
    assert request('GET', '/metrics', auth=False)[0] == 401
    before = snapshot()
    checks.append('metrics is local, ready, and requires the gateway key')
    status, body = request('GET', '/api/documents/example-retrieval-demo')
    assert status == 200
    document = json.loads(body)

    def payload(question):
        status, body = request('POST', '/api/conversations', {'title': 'Public metrics verification'})
        assert status == 201
        return json.loads(body)['id'], {'question': question, 'request_id': 'public-' + uuid4().hex,
            'document_ids': [document['id']], 'focus_document_id': document['id'],
            'focus_source_id': document['source_id']}

    identity, data = payload('fail-public')
    status, body = request('POST', f'/api/conversations/{identity}/turns', data)
    assert status == 200 and json.loads(body)['status'] == 'failed', (status, body)
    assert snapshot()['requests']['observed_business_failures'] == before['requests']['observed_business_failures'] + 1
    checks.append('real JSON turn failure keeps HTTP 200 and increments observed business failure')
    status, body = request('GET', f'/api/conversations/{identity}/messages')
    assert status == 200 and json.loads(body)['turns'][0]['status'] == 'failed'
    checks.append('JSON failure metric agrees with the persisted turn')

    def stream(identity, data):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        try:
            started = time.monotonic()
            connection.request('POST', f'/api/conversations/{identity}/turns/stream', json.dumps(data),
                               {'X-API-Key': key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            assert response.status == 200
            return read_events(response, started)
        finally:
            connection.close()

    failed = stream(*payload('fail-public'))
    assert any(event['event'] == 'error' for event in failed)
    assert failed[-1]['event'] == 'done' and failed[-1]['data']['status'] == 'failed'
    value = snapshot()
    assert value['requests']['observed_business_failures'] == before['requests']['observed_business_failures'] + 2
    assert value['requests']['streams_without_done'] == before['requests']['streams_without_done']
    checks.append('real SSE error/done counts the failed request once without losing terminal recognition')

    success = stream(*payload('Explain public source'))
    result = next(event for event in success if event['event'] == 'result')
    progress = next(event for event in success if event['event'] == 'progress')
    assert success[-1]['data']['status'] == 'completed'
    assert progress['ms'] + 100 < result['ms'], success
    checks.append('actual Agent read_source progress remains incremental before the committed result')
    value = snapshot()
    assert value['requests']['observed_business_failures'] == before['requests']['observed_business_failures'] + 2
    assert value['requests']['http_status'].get('200', 0) >= before['requests']['http_status'].get('200', 0) + 3
    checks.append('HTTP success counts and observed business failure counts remain separate')
    end = time.monotonic() + 3
    while value['requests']['model_http_in_flight'] or value['admission']['held_permits'] or value['admission']['queued']:
        assert time.monotonic() < end
        time.sleep(.02)
        value = snapshot()
    checks.append('model HTTP and admission gauges return to zero after actual responses finish')
    frozen = value['requests']
    assert request('GET', '/livez', auth=False)[0] == 200
    assert request('GET', '/readyz', auth=False)[0] == 200
    final = snapshot()
    assert final['requests'] == frozen
    assert 'synthetic-provider-secret' not in json.dumps(final)
    assert key not in json.dumps(final)
    checks.append('health/metrics polling leaves business counters unchanged and snapshots contain no secrets')
    observations.update(business_failure_delta=2, successful_sse_progress_ms=progress['ms'],
                        successful_sse_result_ms=result['ms'], final_snapshot=final)
    return {'checks_passed': len(checks), 'checks': checks, 'observations': observations,
            'external_model_calls': False, 'model_quality_tested': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.port, args.key)
    args.report.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'checks_passed': result['checks_passed'], 'business_failure_delta': 2}))
