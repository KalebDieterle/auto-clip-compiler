"""An in-memory stand-in for the google-genai client's Files + Batches API, faithful to the shapes
clipcompiler.batch relies on: upload/get/delete/download/list, batches.create/get/cancel, and a
results JSONL in the real REST response format (thought parts, usageMetadata, per-line errors)."""
import itertools
import json
from pathlib import Path
from types import SimpleNamespace as NS


class FakeGemini:
    def __init__(self, judge=None, processing_polls=1):
        self.files = Files(self, processing_polls); self.batches = Batches(self)
        self.judge = judge or (lambda key: {'summary': 's', 'moments': [dict(start=1, end=4, score=8, setup='a', payoff='b',
                                                                              reaction='c', why_funny='bit')]})
        self.error_keys = set()   # keys whose result line is an error
        self.calls = []


class Files:
    def __init__(self, owner, processing_polls):
        self.owner = owner; self.store = {}; self.polls = {}; self.processing_polls = processing_polls
        self.counter = itertools.count(1); self.deleted = []; self.fail_uploads = 0

    def upload(self, file, config=None):
        if self.fail_uploads:
            self.fail_uploads -= 1; raise ConnectionError('upload reset')
        name = f'files/{next(self.counter)}'
        self.store[name] = {'data': Path(file).read_bytes(), 'mime': (config or {}).get('mime_type')}
        self.polls[name] = self.processing_polls
        self.owner.calls.append(('upload', name))
        return NS(name=name, uri=f'https://fake/{name}', state=NS(name='PROCESSING' if self.processing_polls else 'ACTIVE'))

    def get(self, name):
        if name not in self.store: raise KeyError(name)
        self.polls[name] = max(0, self.polls[name]-1)
        return NS(name=name, state=NS(name='PROCESSING' if self.polls[name] else 'ACTIVE'))

    def delete(self, name):
        if name not in self.store: raise KeyError(name)
        del self.store[name]; self.deleted.append(name)

    def download(self, file):
        return self.store[file]['data']

    def list(self):
        return [NS(name=n) for n in self.store]


class Batches:
    def __init__(self, owner):
        self.owner = owner; self.jobs = {}; self.counter = itertools.count(1); self.cancelled = []

    def create(self, model, src, config=None):
        name = f'batches/{next(self.counter)}'
        requests = [json.loads(l) for l in self.owner.files.store[src]['data'].decode('utf-8').splitlines() if l]
        self.jobs[name] = {'model': model, 'requests': requests, 'state': 'JOB_STATE_PENDING', 'dest': None}
        return NS(name=name)

    def get(self, name):
        job = self.jobs[name]
        dest = NS(file_name=job['dest']) if job['dest'] else None
        return NS(name=name, state=NS(name=job['state']), dest=dest)

    def cancel(self, name):
        self.jobs[name]['state'] = 'JOB_STATE_CANCELLED'; self.cancelled.append(name)

    def finish(self, name, state='JOB_STATE_SUCCEEDED'):
        """What Google does in between: judge every request and store a results file."""
        job = self.jobs[name]; job['state'] = state
        if state != 'JOB_STATE_SUCCEEDED': return
        lines = []
        for req in job['requests']:
            uri = req['request']['contents'][0]['parts'][0]['fileData']['fileUri']
            assert uri.replace('https://fake/', '') in self.owner.files.store, 'request points at a deleted upload'
            if req['key'] in self.owner.error_keys:
                lines.append({'key': req['key'], 'error': {'code': 500, 'message': 'internal'}}); continue
            body = json.dumps(self.owner.judge(req['key']))
            lines.append({'key': req['key'], 'response': {
                'candidates': [{'content': {'parts': [{'text': 'thinking...', 'thought': True}, {'text': body}]}}],
                'usageMetadata': {'promptTokenCount': 2850, 'candidatesTokenCount': 200, 'thoughtsTokenCount': 450}}})
        out = f'files/results-{name.split("/")[1]}'
        self.owner.files.store[out] = {'data': '\n'.join(json.dumps(l) for l in lines).encode(), 'mime': 'jsonl'}
        self.owner.files.polls[out] = 0
        job['dest'] = out
