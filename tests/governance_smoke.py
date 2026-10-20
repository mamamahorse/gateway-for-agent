"""Real persisted job lifecycle + worker metrics through a published Gateway."""
import http.client
import json
import time
from uuid import uuid4


def run(port, key, model_port, restart_gateway=None, crash_agent=None):
    checks, observations = [], {}

    def call(method, path, data=None, *, target=port, auth=True, raw=None, content_type='application/json'):
        connection = http.client.HTTPConnection('127.0.0.1', target, timeout=12)
        try:
            connection.request(method, path, raw if raw is not None else json.dumps(data) if data is not None else None,
                {'Content-Type': content_type, **({'X-API-Key': key} if auth else {})})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def request(method, path, data=None, expected=200, **kwargs):
        status, body = call(method, path, data, **kwargs)
        assert status == expected, (method, path, status, body[:800])
        return json.loads(body)

    def until(predicate, message, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(.025)
        raise AssertionError(message)

    def check(message):
        checks.append(message)
        print(message, flush=True)

    def ready():
        try:
            return call('GET', '/readyz', auth=False)[0] == 200
        except OSError:
            return False

    until(ready, 'Gateway/Agent startup failed', 30)
    assert call('GET', '/agent-metrics', auth=False)[0] == 401
    def snapshot():
        return request('GET', '/agent-metrics')
    def idle():
        return until(lambda: snapshot()['held_slots'] == 0, 'Actual Python worker capacity did not return')
    def work_stats():
        return request('GET', '/test/work', target=model_port, auth=False)
    def control(hold='', fail=''):
        request('POST', '/test/control', {'hold': hold, 'fail': fail}, target=model_port, auth=False)
    def held(schema):
        until(lambda: work_stats()[schema]['held'] == 1, f'{schema} model did not enter hold')
    def closed(schema, previous):
        until(lambda: work_stats()[schema]['closed'] == previous + 1, f'{schema} actual model connection did not close')
    assert snapshot()['scope'] == 'python_process'
    check('protected Python process metrics are distinct from local Gateway metrics')

    collection = request('POST', '/api/collections', {'name': 'Public governance ' + uuid4().hex[:8]}, expected=201)['id']
    boundary = 'public-governance-boundary'
    content = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="public.md"\r\n'
               'Content-Type: text/markdown\r\n\r\n# Public evidence\n\nEvidence supports a claim.\n\n'
               f'Recall and explanation use distinct criteria.\r\n--{boundary}--\r\n').encode()
    imported = request('POST', f'/api/documents?collection_id={collection}', expected=201, raw=content,
                       content_type='multipart/form-data; boundary=' + boundary)[0]
    document = request('GET', '/api/documents/' + imported['id'])
    chat = request('POST', '/api/conversations', {'title': '新聊天'}, expected=201)['id']
    turn = {'request_id': uuid4().hex, 'question': 'Explain public evidence', 'document_ids': [document['id']],
            'focus_document_id': document['id'], 'focus_source_id': document['source_id']}
    control(hold='ChatTitle')
    completed = request('POST', f'/api/conversations/{chat}/turns', turn)
    assert completed['status'] == 'completed'
    held('ChatTitle')
    submitted = snapshot()['types']['title']['submitted']
    for _ in range(20):
        request('GET', f'/api/conversations/{chat}/messages')
    state = snapshot()
    assert state['types']['title']['submitted'] == submitted
    assert state['types']['title']['running'] == 1 and state['owned_keys'] == 1
    control()
    idle()
    assert request('GET', f'/api/conversations/{chat}')['title_state'] == 'generated'
    check('JSON title runs in bounded pool; 20 history reads do not duplicate its model work')

    def outline(identity):
        return request('GET', '/api/outlines/' + identity)
    def rows(identity, family):
        data = outline(identity)
        return data['grouping']['reviews'] if family == 'review' else data['grouping']['runs'] if family == 'grouping' else data['routes']['runs']
    def row(identity, family, task):
        return next(value for value in rows(identity, family) if value['id'] == task)
    def create_job(request_id=None):
        return request('POST', f'/api/collections/{collection}/outlines',
                       {'request_id': request_id or uuid4().hex}, expected=202)['id']

    control(hold='FullOutlineDraft')
    payload_id = uuid4().hex
    first = create_job(payload_id)
    held('FullOutlineDraft')
    assert snapshot()['types']['background']['running'] == 1
    assert outline(first)['status'] == 'running'
    previous = work_stats()['FullOutlineDraft']['closed']
    # call() has already closed the 202 submission connection. Detached work
    # must stay alive, unlike an unfinished foreground JSON/SSE request.
    time.sleep(.1)
    assert work_stats()['FullOutlineDraft']['closed'] == previous
    check('HTTP 202 and submitter disconnect preserve actual background ownership')
    if restart_gateway is not None:
        restart_gateway()
        until(ready, 'Gateway replacement did not become ready', 30)
        assert snapshot()['types']['background']['running'] == 1
        assert work_stats()['FullOutlineDraft']['closed'] == previous
        check('replacing only Gateway does not cancel an already accepted Python job')
    assert call('POST', f'/api/collections/{collection}/outlines', {'request_id': 'must-not-be-created'})[0] == 503
    assert call('POST', '/api/ask', {k: value for k, value in turn.items() if k != 'request_id'})[0] == 503
    assert len(request('GET', f'/api/collections/{collection}/outlines')) == 1
    request('GET', '/api/documents')
    check('background and foreground share real capacity; overload rejects before creating another job')
    request('POST', '/api/outlines/' + first + '/pause')
    closed('FullOutlineDraft', previous)
    idle()
    assert outline(first)['status'] == 'paused' and outline(first)['candidates'] == []
    control()
    request('POST', '/api/outlines/' + first + '/resume', expected=202)
    until(lambda: outline(first)['status'] == 'completed', 'Outline resume did not complete')
    idle()
    assert len(outline(first)['candidates']) == 2
    before = snapshot()['types']['background']['submitted']
    assert create_job(payload_id) == first
    assert snapshot()['types']['background']['submitted'] == before
    check('outline pause closes model I/O, rejects late publication, and resume/replay preserve identity')

    control(fail='FullOutlineDraft')
    failed = create_job()
    until(lambda: outline(failed)['status'] == 'failed', 'Business model failure was not persisted')
    idle()
    assert snapshot()['types']['background']['failed'] >= 1
    control()
    request('POST', '/api/outlines/' + failed + '/resume', expected=202)
    until(lambda: outline(failed)['status'] == 'completed', 'Failed outline did not retry')
    idle()
    check('swallowed business model failure is counted from persisted job status and remains resumable')

    control(hold='ComparisonPage')
    grouping = request('POST', '/api/outlines/' + first + '/grouping',
                       {'request_id': uuid4().hex, 'call_limit': 24}, expected=202)['id']
    held('ComparisonPage')
    previous = work_stats()['ComparisonPage']['closed']
    request('POST', '/api/outline-group-runs/' + grouping + '/pause')
    closed('ComparisonPage', previous)
    idle()
    assert row(first, 'grouping', grouping)['status'] == 'paused'
    control(hold='ReviewAction')
    request('POST', '/api/outline-group-runs/' + grouping + '/retry', {'call_limit': 24}, expected=202)
    held('ReviewAction')
    assert row(first, 'grouping', grouping)['status'] == 'completed'
    state = snapshot()
    assert state['types']['background']['running'] == 1 and state['owned_keys'] == 2
    review = next(value for value in rows(first, 'review') if value['status'] == 'running')['id']
    previous = work_stats()['ReviewAction']['closed']
    request('POST', '/api/outline-source-reviews/' + review + '/pause')
    closed('ReviewAction', previous)
    idle()
    assert row(first, 'review', review)['status'] == 'paused'
    check('grouping pause/retry and nested automatic review share one worker until real workflow exit')

    control()
    request('POST', '/api/outline-source-reviews/' + review + '/continue', {'call_limit': 24}, expected=202)
    until(lambda: row(first, 'review', review)['status'] == 'completed', 'Automatic review continue failed')
    idle()
    group = next(value for value in outline(first)['grouping']['suggestions'] if value['run_id'] == grouping)
    control(hold='ReviewAction')
    manual = request('POST', '/api/outline-groups/' + group['id'] + '/source-reviews',
                     {'request_id': uuid4().hex}, expected=202)['id']
    held('ReviewAction')
    previous = work_stats()['ReviewAction']['closed']
    request('POST', '/api/outline-source-reviews/' + manual + '/pause')
    closed('ReviewAction', previous)
    idle()
    control()
    request('POST', '/api/outline-source-reviews/' + manual + '/continue', {'call_limit': 24}, expected=202)
    until(lambda: row(first, 'review', manual)['status'] == 'completed', 'Manual source review did not continue')
    idle()
    check('manual source-review submit/pause/continue retain completed evidence and actual ownership')

    payload = {name: group['proposal'][name] for name in ('name', 'reason', 'canonical_id')}
    payload.update(revision=group['revision'], decision='rejected')
    request('PATCH', '/api/outline-groups/' + group['id'], payload)
    control(hold='RoutePage')
    route = request('POST', '/api/outlines/' + first + '/routes',
                    {'request_id': uuid4().hex, 'call_limit': 24}, expected=202)['id']
    held('RoutePage')
    previous = work_stats()['RoutePage']['closed']
    request('POST', '/api/outline-routes/' + route + '/pause')
    closed('RoutePage', previous)
    idle()
    assert row(first, 'route', route)['status'] == 'paused'
    control()
    request('POST', '/api/outline-routes/' + route + '/continue', {'call_limit': 24}, expected=202)
    until(lambda: row(first, 'route', route)['status'] == 'completed', 'Route did not continue')
    idle()
    check('route submit/pause/continue preserve existing human review and resumable page contracts')

    if crash_agent is not None:
        control(hold='FullOutlineDraft')
        interrupted = create_job()
        held('FullOutlineDraft')
        previous = work_stats()['FullOutlineDraft']['closed']
        calls = work_stats()['FullOutlineDraft']['calls']
        crash_agent()
        until(ready, 'Agent crash replacement did not become ready', 30)
        closed('FullOutlineDraft', previous)
        assert snapshot()['held_slots'] == 0
        assert snapshot()['types']['background']['submitted'] == 0
        assert outline(interrupted)['status'] == 'paused'
        time.sleep(.1)
        assert work_stats()['FullOutlineDraft']['calls'] == calls
        control()
        request('POST', '/api/outlines/' + interrupted + '/resume', expected=202)
        until(lambda: outline(interrupted)['status'] == 'completed', 'Crash-recovered job failed to resume')
        idle()
        check('Agent crash resets process counters; persisted interrupted job recovers and explicitly resumes')

    state = snapshot()
    assert state['held_slots'] == state['queued'] == state['owned_keys'] == 0
    for identity in (first, collection, chat, grouping, manual, route):
        assert identity not in json.dumps(state)
    observations.update(final_snapshot=state, model_work=work_stats())
    check('actual reservations/workers/ownership return to zero and metrics contain no identity labels')
    return {'checks_passed': len(checks), 'checks': checks, 'observations': observations,
            'external_model_calls': False, 'remote_provider_compute_cancellation_verified': False}
