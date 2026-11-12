"""Independent review reproducer inverted to acceptance; offline providers only."""
import json
import threading
import unittest
from unittest.mock import patch
import test_workspace_reconcile as fixtures
import test_workspace_resume_checkpoint as checkpoints
from finix import core

class CancelContract(unittest.TestCase):
    def fixture(self):
        f = fixtures.ReconcileTests('test_lost_ack_reconciles_same_remote_id_and_committed_replay')
        f.setUp(); self.addCleanup(f.doCleanups)
        return f

    def test_profile_cancel_at_upload_never_commits_and_explicit_resume_converges(self):
        f = self.fixture()
        p = checkpoints.WorkspaceResumeCheckpoint('test_same_bytes_valid_owned_checkpoint')
        p.setUp(); self.addCleanup(p.doCleanups)
        cancelled = threading.Event(); fake = f.transport
        def transport(op, *args, **kwargs):
            result = fake(op, *args, **kwargs)
            if op == 'upload': cancelled.set()
            return result
        f.drive.runner = transport
        with patch.object(p.profile, 'workspace', return_value=f.drive):
            with self.assertRaises(core.FinixError):
                p.profile._resume_backup(p.cfg, p.intent, cancel=cancelled.is_set)
            pending = json.loads((f.state/'workspace-pending.json').read_text())
            self.assertEqual(json.loads((f.state/'workspace.json').read_text())['records'], [])
            self.assertFalse(any(op == 'download' for op, _ in fake.calls))
            cancelled.clear()
            result = p.profile._resume_backup(p.cfg, p.intent, cancel=cancelled.is_set)
        self.assertEqual(result['workspace']['id'], pending['record']['id'])
        self.assertEqual(sum(op == 'upload' for op, _ in fake.calls), 1)

    def test_cancel_at_readback_leaves_exact_pending_id(self):
        f = self.fixture(); cancelled = threading.Event(); fake = f.transport
        def transport(op, *args, **kwargs):
            result = fake(op, *args, **kwargs)
            if op == 'download': cancelled.set()
            return result
        f.drive.runner = transport
        with self.assertRaises(core.FinixError):
            f.drive.reconcile(f.path, cancel=cancelled.is_set)
        pending = json.loads((f.state/'workspace-pending.json').read_text())
        self.assertEqual(json.loads((f.state/'workspace.json').read_text())['records'], [])
        cancelled.clear(); f.drive.runner = fake
        self.assertEqual(f.drive.reconcile(f.path)['id'], pending['record']['id'])
        self.assertEqual(sum(op == 'upload' for op, _ in fake.calls), 1)

    def test_public_transport_cooperative_cancel_preserves_pending_and_converges(self):
        """Offline HTTP doubles: sync calls finish before cancellation is observed."""
        from pathlib import Path
        from types import SimpleNamespace
        from urllib.parse import urlsplit, parse_qs
        from finix.workspace_drive import WorkspaceRunner, MIME
        for boundary in ('upload', 'download'):
            with self.subTest(boundary=boundary):
                f = self.fixture(); cancelled = threading.Event(); fake = f.transport
                entered, release = threading.Event(), threading.Event()
                upload = {}; failures = []; active = [True]
                cipher_digest = core.digest_file(f.path)
                def bounded_request():
                    if active[0]:
                        entered.set()
                        if not release.wait(5): raise AssertionError('request fixture timeout')
                def response(body, status=200, headers=None):
                    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                    return SimpleNamespace(status=status, headers=headers or {}, body=raw, json=lambda: json.loads(raw))
                def request(method, url, **kwargs):
                    path = urlsplit(url).path; payload = kwargs.get('json')
                    if '/about' in path:
                        return response({'user': {'permissionId': fake.account}})
                    if path.endswith('/generateIds'):
                        return response({'ids': [fake('generate-id')['id']]})
                    if '/upload/' in path:
                        upload.update(payload)
                        return response({}, headers={'Location': 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=fixture'})
                    if method == 'POST':
                        return response(fake('create-folder', '--id', payload['id'], '--name', payload['name']))
                    obj = path.rsplit('/', 1)[-1]
                    if parse_qs(urlsplit(url).query).get('alt') == ['media']:
                        data = fake.data[obj]
                        if boundary == 'download': bounded_request()
                        start,end = map(int, kwargs['headers']['Range'][6:].split('-'))
                        return response(data[start:end+1], 206, {'Content-Range': f'bytes {start}-{end}/{len(data)}'})
                    try: return response(fake('metadata', obj))
                    except core.FinixError: return response({}, 404)
                class Connection:
                    def __init__(self, *args, **kwargs): pass
                    def set_debuglevel(self, value): pass
                    def request(self, method, target, body, headers):
                        staged = f.root/'synthetic-put.age'; staged.write_bytes(body)
                        self.result = fake('upload', str(staged), '--id', upload['id'], '--name', upload['name'], '--parent', upload['parents'][0])
                    def getresponse(self): return self
                    status = 200
                    def read(self, limit):
                        if boundary == 'upload': bounded_request()
                        return json.dumps({'id': self.result['id'], 'mimeType': MIME}).encode()
                    def close(self): pass
                f.drive.runner = WorkspaceRunner(f.home, request=request)
                def run():
                    try: f.drive.reconcile(f.path, cancel=cancelled.is_set)
                    except BaseException as exc: failures.append(exc)
                with patch('finix.drive.http.client.HTTPSConnection', Connection):
                    worker = threading.Thread(target=run); worker.start()
                    try:
                        self.assertTrue(entered.wait(5), failures)
                        cancelled.set()
                        self.assertTrue(worker.is_alive())  # no claim of interrupting a synchronous SDK call
                    finally:
                        release.set(); worker.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(len(failures), 1)
                    self.assertIsInstance(failures[0], core.FinixError)
                    self.assertIn('cancelled', str(failures[0]))
                    pending = json.loads((f.state/'workspace-pending.json').read_text())
                    self.assertEqual(json.loads((f.state/'workspace.json').read_text())['records'], [])
                    self.assertEqual(pending['record']['sha256'], cipher_digest)
                    self.assertEqual(core.digest_file(f.path), cipher_digest)
                    cancelled.clear(); active[0] = False
                    record = f.drive.reconcile(f.path, cancel=cancelled.is_set)
                    self.assertEqual(record['id'], pending['record']['id'])
                    self.assertEqual(record['sha256'], cipher_digest)
                    self.assertTrue(record['verified'])
                    self.assertEqual(sum(op == 'upload' for op, _ in fake.calls), 1)
                    self.assertFalse((f.state/'workspace-pending.json').exists())

    def test_every_provider_operation_receives_cancel_and_phase_local_progress(self):
        f = self.fixture(); events = []; fake = f.transport
        cancel = lambda: False
        def transport(op, *args, **kwargs):
            self.assertIs(kwargs.get('cancel'), cancel, op)
            if op in {'upload', 'download'}:
                self.assertEqual(events[-1]['phase'], 'upload' if op == 'upload' else 'readback')
                self.assertIsNone(events[-1]['percent'])
                self.assertIsNone(events[-1]['total_bytes'])
            return fake(op, *args, **kwargs)
        f.drive.runner = transport
        self.assertTrue(f.drive.reconcile(f.path, cancel=cancel, progress=events.append)['verified'])
        self.assertTrue(any(e['phase'] == 'readback' and e['percent'] == 100 for e in events))

if __name__ == '__main__': unittest.main()
