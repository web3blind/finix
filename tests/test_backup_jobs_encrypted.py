"""Generated age encryption + actual orchestration; cloud is in-memory only."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from finix import core, streaming
from finix.backup_jobs import BackupJobs, _busy
from finix.integration import Profile
from finix.setup_flow import SetupFlow

class EncryptedBackupJobs(unittest.TestCase):
    def test_generated_encrypted_complete_incomplete_and_real_progress(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            base = Path(tmp)
            home = core.private_dir(base / 'source')
            (home / 'good').write_bytes(b'synthetic-work-' * 3000)
            (home / 'omittable').write_bytes(b'only-fixture')
            identity = base / 'identity'
            recipient = core.keygen(identity)
            profile = Profile(home, default_root=home)
            cfg = profile.config()
            cfg.update(recipient=recipient, storage='rclone', retention=1,
                       rclone_config=str(base/'synthetic.conf'), rclone_remote='fake', rclone_root='fake')
            core.atomic_json(profile.root / 'settings.json', cfg)
            events, objects, keeps = [], {}, []
            class Cloud:
                def require_no_pending(self): pass
                def backup_cipher(self, path, keep, *, completion=None, progress=None, cancel=None):
                    keeps.append(keep)
                    data = path.read_bytes()
                    objects[path.name] = data
                    if progress:
                        progress(dict(phase='upload', completed_bytes=len(data), total_bytes=len(data), percent=100))
                        progress(dict(phase='verify', completed_bytes=len(data), total_bytes=len(data), percent=100))
                    if objects[path.name] != data: raise AssertionError('readback')
                    return dict(verified=True, sha256=core.digest_file(path))
            def flow(p):
                instance = SetupFlow(p)
                method = instance.check_and_run
                def run(progress=None, **kwargs):
                    def observe(value):
                        events.append(value)
                        if progress: progress(value)
                    return method(progress=observe, **kwargs)
                instance.check_and_run = run
                return instance
            jobs = BackupJobs(flow_factory=flow)
            def finish(request):
                jobs.start(profile, profile.profile_id, request)
                for _ in range(1000):
                    result = jobs.current(Profile(home, default_root=home))['job']
                    if result['state'] != 'running':
                        # Terminal public state is written just before lease release.
                        for _ in range(100):
                            if not _busy(profile.root/'backup-jobs'/'worker.lock'): break
                            time.sleep(.01)
                        return result
                    time.sleep(.01)
                self.fail('worker did not finish')
            with patch.object(Profile, 'rclone', return_value=Cloud()), \
                 patch('finix.rclone_storage.RcloneRunner.__call__', side_effect=AssertionError('NO REAL PROVIDER')):
                complete = finish('11111111-1111-4111-8111-111111111111')
                self.assertEqual(complete['state'], 'succeeded', complete)
                self.assertTrue(complete['verified'])
                payload_events = [e for e in events if e.get('phase') == 'snapshot' and 'completed_files' in e]
                self.assertEqual(payload_events[-1]['completed_files'], 2)
                self.assertEqual(payload_events[-1]['completed_bytes'], sum(p.stat().st_size for p in home.iterdir()))
                self.assertTrue(any(e.get('phase') == 'upload' and e.get('percent') == 100 for e in events))
                archive = next((profile.root/'copies').glob('*.age'))
                restored = base / 'restored'
                core.restore(archive, identity, restored)
                self.assertEqual((restored/'good').read_bytes(), (home/'good').read_bytes())
                original = streaming.file_payload
                @contextmanager
                def omit(rootfd, name, *args, **kwargs):
                    if name == 'omittable': raise core.SourceCaptureError('LIVE_SOURCE_MISSING')
                    with original(rootfd, name, *args, **kwargs) as value: yield value
                with patch.object(streaming, 'file_payload', omit):
                    incomplete = finish('22222222-2222-4222-8222-222222222222')
                self.assertEqual(incomplete['state'], 'incomplete', incomplete)
                self.assertTrue(incomplete['verified'])
                self.assertFalse(incomplete['complete'])
                self.assertEqual(incomplete['omitted_count'], 1)
                self.assertEqual(keeps, [1, 0])
                self.assertTrue(archive.exists())
                self.assertFalse(SetupFlow(profile).view()['verified'])
                with self.assertRaises(core.FinixError): SetupFlow(profile).schedule('24h')
                safe = str(jobs.current(profile))
                self.assertNotIn(str(home), safe)
                self.assertNotIn(recipient, safe)

    def test_real_encryption_through_existing_memory_runner_ledger(self):
        from finix.rclone_storage import RcloneStorage
        from tests.test_rclone_storage import MemoryRunner
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            base = Path(tmp)
            home = core.private_dir(base/'home')
            (home/'work').write_bytes(b'actual generated encrypted local work')
            auth = core.private_dir(base/'auth')
            config = auth/'rclone.conf'
            config.write_text('[cloud]\ntype = yandex\n')
            config.chmod(0o600)
            identity = base/'identity'
            profile = Profile(home, default_root=home)
            cfg = profile.config()
            cfg.update(recipient=core.keygen(identity), storage='rclone', retention=1,
                       rclone_config=str(config), rclone_remote='cloud', rclone_root='backups/Finix')
            core.atomic_json(profile.root/'settings.json', cfg)
            runner = MemoryRunner()
            class Adapter(RcloneStorage):
                # Baseline transport lacks the child candidate's optional args.
                # Still exercise its real pending/ledger/full readback boundary.
                def backup_cipher(self, path, keep=0, *, completion=None, progress=None, cancel=None):
                    return super().backup_cipher(path, keep, completion=completion)
            adapter = Adapter(home, profile.profile_id, profile.root/'rclone',
                              str(config), 'cloud', 'backups/Finix', runner=runner)
            jobs = BackupJobs()
            with patch.object(Profile, 'rclone', return_value=adapter), \
                 patch('finix.rclone_storage.RcloneRunner.__call__', side_effect=AssertionError('NO NETWORK')):
                jobs.start(profile, profile.profile_id, '11111111-1111-4111-8111-111111111111')
                for _ in range(500):
                    result = jobs.current(profile)['job']
                    if result['state'] != 'running': break
                    time.sleep(.01)
                while _busy(profile.root/'backup-jobs'/'worker.lock'): time.sleep(.005)
                self.assertEqual(result['state'], 'succeeded', result)
                self.assertTrue(result['verified'])
                self.assertEqual(len(adapter.ledger()['records']), 1)
                row = adapter.ledger()['records'][0]
                with adapter._locked():
                    self.assertIsNone(adapter.pending(adapter.ledger()))
                downloaded = base/'downloaded.age'
                adapter.download(row, downloaded)
                core.restore(downloaded, identity, base/'restore')
                self.assertEqual((base/'restore'/'work').read_bytes(), (home/'work').read_bytes())
                runner.corrupt = True
                jobs.start(profile, profile.profile_id, '22222222-2222-4222-8222-222222222222')
                for _ in range(500):
                    failed = jobs.current(profile)['job']
                    if failed['state'] != 'running': break
                    time.sleep(.01)
                while _busy(profile.root/'backup-jobs'/'worker.lock'): time.sleep(.005)
                self.assertEqual(failed['state'], 'failed', failed)
                with adapter._locked():
                    intent_before = adapter.pending(adapter.ledger())
                self.assertIsNotNone(intent_before)
                snapshots = list((profile.root/'copies').glob('*.age'))
                runner.corrupt = False
                with patch.object(core, 'backup', side_effect=AssertionError('NO NEW SNAPSHOT')):
                    jobs.start(profile, profile.profile_id, '33333333-3333-4333-8333-333333333333')
                    for _ in range(500):
                        resumed = jobs.current(profile)['job']
                        if resumed['state'] != 'running': break
                        time.sleep(.01)
                    while _busy(profile.root/'backup-jobs'/'worker.lock'): time.sleep(.005)
                self.assertEqual(resumed['state'], 'succeeded', resumed)
                self.assertEqual(resumed['id'], failed['id'])
                self.assertEqual(list((profile.root/'copies').glob('*.age')), snapshots)
                self.assertIn(intent_before['id'], [r['id'] for r in adapter.ledger()['records']])

    def test_failed_upload_reconciles_same_cipher_without_new_snapshot(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            base = Path(tmp)
            home = core.private_dir(base / 'source')
            (home/'work').write_bytes(b'synthetic durable work')
            identity = base/'identity'
            profile = Profile(home, default_root=home)
            cfg = profile.config()
            cfg.update(recipient=core.keygen(identity), storage='rclone',
                       rclone_config=str(base/'fake.conf'), rclone_remote='fake', rclone_root='fake')
            core.atomic_json(profile.root/'settings.json', cfg)
            calls, pending, reconciled = [], [], []
            class Cloud:
                def require_no_pending(self):
                    if pending: raise AssertionError('must reconcile before capture')
                def backup_cipher(self, path, keep, *, completion=None, **kwargs):
                    calls.append(path)
                    pending.append(dict(sha256=core.digest_file(path)))
                    raise core.FinixError('private provider stderr /never/public')
                def ledger(self): return {'records': []}
                def _locked(self):
                    from contextlib import nullcontext
                    return nullcontext()
                def pending(self, journal): return pending[0]
                def reconcile(self, path, **kwargs):
                    reconciled.append(path)
                    if core.digest_file(path) != pending[0]['sha256']: raise AssertionError('wrong cipher')
                    return dict(verified=True)
            jobs = BackupJobs()
            def finish(request):
                result = jobs.start(profile, profile.profile_id, request)
                for _ in range(500):
                    result = jobs.current(profile)
                    if result['job']['state'] != 'running':
                        while _busy(profile.root/'backup-jobs'/'worker.lock'): time.sleep(.005)
                        return result
                    time.sleep(.01)
                self.fail('job timeout')
            with patch.object(Profile, 'rclone', return_value=Cloud()), \
                 patch('finix.rclone_storage.RcloneRunner.__call__', side_effect=AssertionError('NO PROVIDER')):
                failed = finish('11111111-1111-4111-8111-111111111111')
                self.assertEqual(failed['job']['state'], 'failed')
                self.assertNotIn('private provider', str(failed))
                # New request retries transport only; all capture entry points forbidden.
                with patch.object(core, 'backup', side_effect=AssertionError('NO NEW SNAPSHOT')):
                    succeeded = finish('22222222-2222-4222-8222-222222222222')
                self.assertEqual(succeeded['job']['state'], 'succeeded', succeeded)
                self.assertEqual(failed['job']['id'], succeeded['job']['id'])
                self.assertEqual(calls, reconciled)
                self.assertEqual(len(calls), 1)
                restored = base/'restored'
                core.restore(calls[0], identity, restored)
                self.assertEqual((restored/'work').read_bytes(), (home/'work').read_bytes())

if __name__ == '__main__': unittest.main()
