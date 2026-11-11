"""Composed authenticated-router fixture; provider calls are forbidden."""
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

class BackupHTTP(unittest.TestCase):
    def test_202_timeout_reattach_ten_polls_and_profile_fence(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        path = Path(__file__).parents[1] / 'dashboard' / 'plugin_api.py'
        spec = importlib.util.spec_from_file_location('backup_jobs_http_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        core = __import__(module._package + '.core', fromlist=['core'])
        release, entered = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = core.private_dir(Path(tmp) / 'state')
            profile = SimpleNamespace(root=root, profile_id='b'*64,
                                      config=lambda: {'storage':'rclone'})
            calls = []
            class Flow:
                def _binding(self): return 'binding'
                def check_and_run(self, **kwargs):
                    calls.append(1)
                    entered.set()
                    release.wait(5)
                    return {'verified': True, 'first_backup': {'complete':True,'omitted_count':0}}
            module._backup_jobs.flow_factory = lambda p: Flow()
            app = FastAPI()
            app.include_router(module.router)
            try:
                with patch.object(module, 'current_profile', return_value=profile), TestClient(app) as client:
                    body = dict(profile_id=profile.profile_id, request_id='11111111-1111-4111-8111-111111111111')
                    before = time.monotonic()
                    response = client.post('/backup/jobs', json=body)
                    self.assertEqual(response.status_code, 202, response.text)
                    self.assertLess(time.monotonic()-before, 1)
                    identifier = response.json()['job']['id']
                    self.assertEqual(response.json()['code_version'], 'backup-jobs-v1')
                    self.assertTrue(entered.wait(1))
                    # Browser closes/reopens after losing its response: status is durable.
                    with TestClient(app) as reopened:
                        for _ in range(10):
                            poll = reopened.get('/backup/jobs/current')
                            self.assertEqual(poll.status_code, 200)
                            self.assertEqual(poll.json()['job']['id'], identifier)
                            self.assertEqual(poll.json()['job']['state'], 'running')
                        again = reopened.post('/backup/jobs', json=body)
                        self.assertEqual(again.json()['job']['id'], identifier)
                        wrong = reopened.post('/backup/jobs', json={**body, 'profile_id':'other'})
                        self.assertEqual(wrong.status_code, 409)
                    self.assertLess(time.monotonic()-before, 2)
                    self.assertEqual(calls, [1])
                    release.set()
                    for _ in range(200):
                        final = client.get('/backup/jobs/current').json()
                        if final['job']['state'] != 'running': break
                        time.sleep(.01)
                    self.assertEqual(final['job']['state'], 'succeeded')
                    self.assertNotIn(str(root), str(final))
                    self.assertEqual(set(final['job']), set(('id','state','phase','progress','started_at',
                        'updated_at','error_code','verified','complete','omitted_count')))
            finally:
                release.set()
                # Ensure worker releases its file handles before native cleanup.
                from finix.backup_jobs import _busy
                for _ in range(200):
                    if not _busy(root / 'backup-jobs' / 'worker.lock'): break
                    time.sleep(.01)

if __name__ == '__main__': unittest.main()
