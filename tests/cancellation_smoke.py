"""Verify actual model EOF, saved turn state and new work after cancellation."""
import http.client
import json
import time
from uuid import uuid4


def run(port, key, model_port):
    checks, observations = [], {}
    def request(method, path, data=None, target=port, auth=True):
        connection = http.client.HTTPConnection('127.0.0.1', target, timeout=8)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None,
                {'Content-Type': 'application/json', **({'X-API-Key': key} if auth else {})})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def until(predicate, message, seconds=4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        raise AssertionError(message)

    until(lambda: request('GET', '/readyz', auth=False)[0] == 200, 'readiness timeout', 20)
    document = json.loads(request('GET', '/api/documents/example-retrieval-demo')[1])
    def new(mode):
        identity = json.loads(request('POST', '/api/conversations', {'title': 'Public cancellation test'})[1])['id']
        marker = f'cancel-{mode}-public-{uuid4().hex}'
        data = {'question': marker, 'request_id': uuid4().hex, 'document_ids': [document['id']],
                'focus_document_id': document['id'], 'focus_source_id': document['source_id']}
        return identity, data

    def stats(marker):
        return json.loads(request('GET', '/test/stats', target=model_port, auth=False)[1]).get(marker, {})

    def saved(identity):
        body = json.loads(request('GET', f'/api/conversations/{identity}/messages')[1])
        return body['turns']

    def cancelled_state(identity, marker):
        until(lambda: stats(marker).get('closed') == 1, 'actual model HTTP socket did not close')
        result = until(lambda: (turns if len(turns := saved(identity)) == 1 and turns[0]['status'] == 'failed' else None),
                       'cancelled turn was not saved as failed')
        assert result[0]['error'] == '生成已取消，原始输入已保存，可以重试。', result
        assert len(result) == 1
        before = stats(marker)['calls']
        time.sleep(.1)
        assert stats(marker)['calls'] == before, 'cancelled Agent continued to another model call'
        return result[0]

    def disconnect(mode, stream=True):
        identity, data = new(mode)
        path = f'/api/conversations/{identity}/turns' + ('/stream' if stream else '')
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=8)
        response = None
        try:
            connection.request('POST', path, json.dumps(data), {'X-API-Key': key, 'Content-Type': 'application/json'})
            if stream:
                response = connection.getresponse()
                assert response.status == 200
                # Do not close merely after meta: prove the real SDK call is
                # currently holding its HTTP socket, including final generation.
            until(lambda: stats(data['question']).get('started') == 1, 'model did not enter held exchange')
            start = time.monotonic()
            if response is not None:
                response.close()
            connection.close()
            turn = cancelled_state(identity, data['question'])
            elapsed = round((time.monotonic() - start) * 1000)
            assert elapsed < 2000, ('disconnect did not promptly end local work', elapsed)
            return {'mode': mode, 'stream': stream, 'cancel_and_saved_ms': elapsed,
                    'model': stats(data['question']), 'turn_status': turn['status']}
        finally:
            if response is not None:
                response.close()
            connection.close()

    observations['sse_headers'] = disconnect('headers')
    checks.append('SSE disconnect closes actual model header wait and saves one failed turn')
    observations['sse_body'] = disconnect('body')
    checks.append('SSE disconnect aborts partial model response body without SDK retry')
    observations['sse_final'] = disconnect('final')
    assert observations['sse_final']['model']['phase'] == 'final'
    checks.append('SSE disconnect during final structured generation prevents late completed publication')
    observations['json_headers'] = disconnect('headers', stream=False)
    checks.append('JSON disconnect is observed before response headers and cancels actual work')

    identity, data = new('timeout')
    status, body = request('POST', f'/api/conversations/{identity}/turns', data)
    assert status == 504, (status, body)
    cancelled_state(identity, data['question'])
    checks.append('Gateway JSON total deadline propagates cancellation to actual model I/O and saved turn')

    identity, data = new('idle')
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=8)
    try:
        connection.request('POST', f'/api/conversations/{identity}/turns/stream', json.dumps(data),
                           {'X-API-Key': key, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 200
        try:
            response.read()
        except http.client.IncompleteRead:
            pass
        response.close()
    finally:
        connection.close()
    cancelled_state(identity, data['question'])
    checks.append('Gateway SSE idle deadline closes Python stream and actual model request')

    observations['repeated'] = [disconnect('headers', stream=index % 2 == 0) for index in range(12)]
    checks.append('12 repeated alternating JSON/SSE cancellations leave one failed turn each and no further model calls')

    identity, data = new('headers')
    data['question'] = 'Explain public source after cancellations'
    until(lambda: json.loads(request('GET', '/metrics')[1])['admission']['held_permits'] == 0,
          'proxy capacity did not recover')
    def submit_success():
        status, body = request('POST', f'/api/conversations/{identity}/turns', data)
        if status == 503:
            return None
        assert status == 200, (status, body)
        value = json.loads(body)
        assert value['status'] == 'completed', value
        return value
    completed = until(submit_success, 'actual worker capacity did not recover')
    status, body = request('POST', f'/api/conversations/{identity}/turns', data)
    assert status == 200 and json.loads(body) == completed
    assert saved(identity) == [completed]
    checks.append('actual worker accepts new generation after cancellation; completed replay remains idempotent')
    snapshot = json.loads(request('GET', '/metrics')[1])
    assert snapshot['requests']['model_http_in_flight'] == 0
    assert snapshot['admission']['held_permits'] == snapshot['admission']['queued'] == 0
    observations['final_snapshot'] = snapshot
    checks.append('proxy in-flight and queue gauges return to zero after actual cancellation/recovery checks')
    return {'checks_passed': len(checks), 'checks': checks, 'observations': observations,
            'external_model_calls': False, 'remote_provider_compute_cancellation_verified': False}
