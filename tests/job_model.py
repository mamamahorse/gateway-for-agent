"""Controlled outline/title/model I/O, with observable holds and EOF; no network clients."""
import io
import json
import re
import select
import time

from cancel_model import CancelModelHandler, initialize as initialize_cancel


SCHEMAS = {'FullOutlineDraft', 'OutlineDraft', 'DocumentBrief', 'ComparisonPage', 'GroupDraft',
           'ReviewAction', 'ReviewConclusion', 'RoutePage', 'ChatTitle'}


def initialize(server):
    initialize_cancel(server)
    server.work_control = {'hold': '', 'fail': ''}
    server.work_stats = {name: {'calls': 0, 'held': 0, 'closed': 0} for name in sorted(SCHEMAS)}
    return server


def citation(data):
    if 'sources' in data:
        source = data['sources']
        identity = re.search(r'Source ID: ([^\n]+)', source)[1]
        start = int(re.search(r'Visible lines: L(\d+)', source)[1])
        return {'source_id': identity, 'line_start': start, 'line_end': start}
    if 'original_excerpts' in data:
        return {key: data['original_excerpts'][0][key] for key in ('source_id', 'line_start', 'line_end')}
    return data['overviews'][0]['points'][0]['citations'][0]


def result_for(name, body):
    data = json.loads(body['messages'][1]['content'])
    if name == 'ChatTitle':
        return {'title': 'Public controlled title'}
    if name in ('FullOutlineDraft', 'OutlineDraft'):
        cite = citation(data)
        return {'proposals': [{'knowledge': {'name': 'Public evidence', 'statement': 'Evidence supports a claim.',
                   'content_tags': [], 'citations': [cite]},
                   'target': {'title': f'{capability} evidence', 'capability': capability,
                   'description': 'Explain the supplied public evidence.', 'scope': 'Only this public example.',
                   'criteria': ['Identify the evidence', 'Connect evidence to a claim']}}
                   for capability in ('recall', 'explain')], 'note': 'Synthetic lifecycle verification only.'}
    if name == 'DocumentBrief':
        return {'points': [{'text': 'Public evidence supports a claim.', 'citations': [citation(data)]}], 'unresolved': []}
    if name in ('ComparisonPage', 'GroupDraft'):
        candidates = [row['id'] for row in data['candidates']]
        result = {'suggestions': [{'kind': 'needs_review', 'candidate_ids': candidates[:2],
                   'name': 'Public evidence', 'reason': 'Controlled candidate relationship needs review.',
                   'canonical_id': None}], 'note': 'Synthetic grouping only.'}
        if name == 'ComparisonPage':
            result.update(checked_indices=[pair['index'] for pair in data['comparisons']], deferred_indices=[])
        return result
    if name in ('ReviewAction', 'ReviewConclusion'):
        return {'action': 'conclude', 'issue': 'Public controlled uncertainty', 'requests': [],
                'outcome': 'unresolved', 'name': 'Public evidence', 'reason': 'Synthetic review retains uncertainty.',
                'canonical_id': None, 'citations': [], 'clarifications': []}
    if name == 'RoutePage':
        return {'comparisons': [{'pair_index': pair['index'], 'relation': 'a_before_b', 'basis': 'source',
                'certainty': 'tentative', 'reason': 'Public prior concept supports the next target.',
                'citations': [citation(data)]} for pair in data['comparisons']],
                'missing_topics': [], 'note': 'Synthetic route only.'}
    raise ValueError('Unexpected controlled schema')


class JobModelHandler(CancelModelHandler):
    def do_GET(self):
        if self.path == '/test/work':
            with self.server.lock:
                body = json.dumps(self.server.work_stats).encode()
            return self.reply(200, body)
        return super().do_GET()

    def reply(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        if self.path == '/test/control':
            assert body.get('hold', '') in SCHEMAS | {''} and body.get('fail', '') in SCHEMAS | {''}
            with self.server.lock:
                self.server.work_control = {'hold': body.get('hold', ''), 'fail': body.get('fail', '')}
            return self.reply(200, b'{}')
        name = body.get('response_format', {}).get('json_schema', {}).get('name')
        if name not in SCHEMAS:
            original = self.rfile
            self.rfile = io.BytesIO(raw)
            try:
                return super().do_POST()
            finally:
                self.rfile = original
        with self.server.lock:
            row = self.server.work_stats[name]
            row['calls'] += 1
            holding = self.server.work_control['hold'] == name
            failing = self.server.work_control['fail'] == name
            if holding:
                row['held'] += 1
        if holding:
            try:
                end = time.monotonic() + 15
                while time.monotonic() < end:
                    if select.select([self.connection], [], [], .01)[0]:
                        try:
                            closed = self.connection.recv(1) == b''
                        except ConnectionResetError:
                            closed = True
                        if closed:
                            with self.server.lock:
                                row['closed'] += 1
                            self.close_connection = True
                            return
                    with self.server.lock:
                        if self.server.work_control['hold'] != name:
                            break
            finally:
                with self.server.lock:
                    row['held'] -= 1
        if failing:
            return self.reply(400, b'{"error":{"message":"synthetic-private-provider-error","type":"invalid_request_error"}}')
        result = result_for(name, body)
        data = {'id': 'public', 'object': 'chat.completion', 'created': 0, 'model': 'controlled',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': json.dumps(result)}}]}
        return self.reply(200, json.dumps(data).encode())
