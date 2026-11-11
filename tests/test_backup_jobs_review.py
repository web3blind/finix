"""Offline regressions for independent durable-job review findings."""
import contextvars
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from finix import core
from finix.backup_jobs import BackupJobs, _busy


def finish(jobs, profile, request=None):
    jobs.start(profile, profile.profile_id, request or str(uuid.uuid4()))
    for _ in range(1000):
        result = jobs.current(profile)['job']
        if result['state'] != 'running':
            while _busy(profile.root/'backup-jobs'/'worker.lock'): time.sleep(.002)
            return result
        time.sleep(.002)
    raise AssertionError('worker stuck')


class ReviewRegressions(unittest.TestCase):
    def fixture(self, root):
        from finix.integration import Profile
        from finix.rclone_storage import RcloneStorage
        from tests.test_rclone_storage import MemoryRunner
        home = core.private_dir(root/'source')
        (home/'work').write_bytes(b'generated offline fixture'*100)
        auth = core.private_dir(root/'auth')
        config = auth/'rclone.conf'
        config.write_text('[cloud]\ntype=yandex\n')
        config.chmod(0o600)
        p = Profile(home, default_root=home)
        cfg = p.config()
        cfg.update(storage='rclone', recipient=core.keygen(root/'generated.key'), rclone_config=str(config), rclone_remote='cloud', rclone_root='Finix', retention=0)
        core.atomic_json(p.root/'settings.json', cfg)
        runner = MemoryRunner()
        adapter = RcloneStorage(home, p.profile_id, p.root/'rclone', str(config), 'cloud', 'Finix', runner=runner)
        return p, adapter, runner, config

    def test_checkpoint_before_pending_retries_exact_ciphertext(self):
        from finix.integration import Profile
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            p, adapter, runner, config = self.fixture(Path(tmp))
            jobs = BackupJobs()
            with patch.object(Profile, 'rclone', return_value=adapter):
                with patch.object(adapter, 'backup_cipher', side_effect=core.FinixError('synthetic before intent')):
                    first = finish(jobs, p)
                self.assertEqual(first['state'], 'failed')
                before = {f.name: core.digest_file(f) for f in (p.root/'copies').glob('*.age')}
                # Baseline transport accepts no callbacks; isolate this finding.
                backup = adapter.backup_cipher
                def transport(path, keep=0, *, completion=None, **kw):
                    return backup(path, keep, completion=completion)
                with patch.object(adapter, 'backup_cipher', side_effect=transport), patch.object(core, 'backup', side_effect=AssertionError('NO RECAPTURE')):
                    second = finish(jobs, p)
                self.assertEqual(second['state'], 'succeeded', second)
                self.assertEqual(first['id'], second['id'])
                self.assertEqual(before, {f.name: core.digest_file(f) for f in (p.root/'copies').glob('*.age')})
                self.assertEqual(len(adapter.ledger()['records']), 1)

    def test_pre_intent_checkpoint_cannot_create_under_replaced_namespace(self):
        from finix.integration import Profile
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            p, adapter, runner, config = self.fixture(Path(tmp))
            jobs = BackupJobs()
            with patch.object(Profile, 'rclone', return_value=adapter):
                with patch.object(adapter, 'backup_cipher', side_effect=core.FinixError('before intent')):
                    self.assertEqual(finish(jobs,p)['state'],'failed')
                with adapter._locked():
                    ledger=adapter.ledger();ledger['namespace']=uuid.uuid4().hex;adapter._write('rclone.json',ledger)
                before=len([c for c in runner.calls if c[0]=='copyto'])
                result=finish(jobs,p)
                self.assertEqual(result['state'],'failed',result)
                self.assertEqual(len([c for c in runner.calls if c[0]=='copyto']),before)

    def test_oauth_refresh_metadata_does_not_rebind_but_connection_change_does(self):
        from finix.integration import Profile
        from finix.setup_flow import SetupFlow
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            p, adapter, runner, config = self.fixture(Path(tmp))
            before = SetupFlow(p)._binding()
            class RefreshCloud:
                def require_no_pending(self): pass
                def backup_cipher(self, path, *args, **kw):
                    replacement = config.with_suffix('.refresh')
                    replacement.write_text('[cloud]\ntype=yandex\ntoken=synthetic-refresh\n')
                    replacement.chmod(0o600)
                    os.replace(replacement, config)
                    return {'verified': True, 'sha256': core.digest_file(path)}
            with patch.object(Profile, 'rclone', return_value=RefreshCloud()):
                self.assertEqual(finish(BackupJobs(), p)['state'], 'succeeded')
            self.assertEqual(SetupFlow(p)._binding(), before)
            cfg = p.config(); cfg['rclone_remote'] = 'explicit-other-connection'
            core.atomic_json(p.root/'settings.json', cfg)
            self.assertNotEqual(SetupFlow(p)._binding(), before)

    def test_129_jobs_keep_old_replays_without_duplicate_work(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            p = SimpleNamespace(root=core.private_dir(Path(tmp)/'jobs'), profile_id='c'*64)
            calls = []
            class Flow:
                def _binding(self): return 'binding'
                def check_and_run(self, **kw):
                    calls.append(1)
                    return {'verified': True, 'first_backup': {'complete': True, 'omitted_count': 0}}
            jobs = BackupJobs(lambda p: Flow())
            requests = [str(uuid.uuid4()) for _ in range(129)]
            results = [finish(jobs, p, request) for request in requests]
            self.assertEqual(len(calls), 129)
            for request, result in zip(requests, results):
                replay = BackupJobs(lambda p: self.fail('replay started work')).start(p, p.profile_id, request)['job']
                self.assertEqual(replay['id'], result['id'])
                self.assertEqual(replay['state'], 'succeeded')
            self.assertEqual(len(calls), 129)

    def test_authenticated_context_is_copied_per_worker_not_reused(self):
        selector = contextvars.ContextVar('authenticated_home', default='process-A')
        host = None
        if os.environ.get('FINIX_TEST_HOST_CONSTANTS'):
            spec = importlib.util.spec_from_file_location('synthetic_host_constants', os.environ['FINIX_TEST_HOST_CONSTANTS'])
            host = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(host)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            p = SimpleNamespace(root=core.private_dir(root/'jobs'), profile_id='b'*64)
            observed = []
            class Flow:
                def _binding(self): return 'binding'
                def check_and_run(self, **kwargs):
                    observed.append((selector.get(), str(host.get_hermes_home()) if host else None))
                    selector.set('worker-only')
                    return {'verified': True, 'first_backup': {'complete': True, 'omitted_count': 0}}
            jobs = BackupJobs(lambda p: Flow())
            with patch.dict(os.environ, {'HERMES_HOME': str(root/'process-A')}):
                for name in ('request-B', 'request-C'):
                    token = selector.set(name)
                    htoken = host.set_hermes_home_override(root/name) if host else None
                    try:
                        self.assertEqual(finish(jobs, p)['state'], 'succeeded')
                        self.assertEqual(selector.get(), name)
                    finally:
                        selector.reset(token)
                        if host: host.reset_hermes_home_override(htoken)
            self.assertEqual(observed, [(name, str(root/name) if host else None) for name in ('request-B','request-C')])
