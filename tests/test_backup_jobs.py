"""Offline regression for long-running setup: no provider executables."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from finix import core


class BackupJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'state'
        core.private_dir(root)
        self.profile = SimpleNamespace(root=root, profile_id='a'*64,
                                       config=lambda: {'storage': 'rclone'})

    def test_start_and_ten_polls_return_while_twenty_minute_worker_active(self):
        from finix.backup_jobs import BackupJobs
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Flow:
            def _binding(self): return 'binding'
            def check_and_run(self, **kwargs):
                entered.set()
                if not release.wait(5): raise AssertionError('fixture release missing')
                return {'verified': True, 'first_backup': {'complete': True, 'omitted_count': 0}}
        jobs = BackupJobs(flow_factory=lambda p: Flow())
        before = time.monotonic()
        result = jobs.start(self.profile, self.profile.profile_id, '11111111-1111-4111-8111-111111111111')
        self.assertLess(time.monotonic()-before, 1)
        self.assertTrue(entered.wait(1))
        identifier = result['job']['id']
        # Advance a simulated wall clock twenty minutes without waiting or
        # mistaking a long transfer for a dead worker.
        clock = SimpleNamespace(time=lambda: result['job']['started_at'] + 1200,
                                monotonic=time.monotonic, sleep=time.sleep)
        with patch('finix.backup_jobs.time', clock):
            for _ in range(10):
                poll = jobs.current(self.profile)
                self.assertEqual(poll['job']['id'], identifier)
                self.assertEqual(poll['job']['state'], 'running')
        self.assertLess(time.monotonic()-before, 1)
        duplicate = jobs.start(self.profile, self.profile.profile_id, '22222222-2222-4222-8222-222222222222')
        self.assertEqual(duplicate['job']['id'], identifier)
        self.assertEqual(BackupJobs().current(self.profile)['job']['state'], 'running')
        import subprocess, sys, json
        code = ('import sys,json; from pathlib import Path; from types import SimpleNamespace; '
                'from finix.backup_jobs import BackupJobs; '
                'p=SimpleNamespace(root=Path(sys.argv[1]),profile_id=sys.argv[2]); '
                'print(json.dumps(BackupJobs().start(p,p.profile_id,"33333333-3333-4333-8333-333333333333")))')
        other = subprocess.run([sys.executable, '-c', code, str(self.profile.root), self.profile.profile_id],
                               capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(json.loads(other.stdout)['job']['id'], identifier)
        release.set()
        for _ in range(100):
            final = jobs.current(self.profile)
            if final['job']['state'] != 'running': break
            time.sleep(.01)
        self.assertEqual(final['job']['state'], 'succeeded')
        self.assertTrue(final['job']['verified'])
        self.assertEqual(jobs.start(self.profile, self.profile.profile_id, '11111111-1111-4111-8111-111111111111')['job']['id'], identifier)

    def test_start_failure_and_capacity_are_bounded_without_leaked_slots(self):
        from finix.backup_jobs import BackupJobs, _SLOTS, _busy
        request = '11111111-1111-4111-8111-111111111111'
        class Broken:
            def _binding(self): raise RuntimeError('private path must not persist')
        jobs = BackupJobs(flow_factory=lambda p: Broken())
        for _ in range(6):
            with self.assertRaises(RuntimeError): jobs.start(self.profile, self.profile.profile_id, request)
        self.assertIsNone(jobs.current(self.profile)['job'])
        held = []
        try:
            for _ in range(4):
                self.assertTrue(_SLOTS.acquire(blocking=False))
                held.append(True)
            with self.assertRaisesRegex(core.FinixError, 'JOB_CAPACITY'):
                jobs.start(self.profile, self.profile.profile_id, request)
        finally:
            for _ in held: _SLOTS.release()
        class Flow:
            def _binding(self): return 'binding'
            def check_and_run(self, **kwargs):
                raise RuntimeError('private provider stderr /credentials')
        jobs = BackupJobs(flow_factory=lambda p: Flow())
        jobs.start(self.profile, self.profile.profile_id, request)
        for _ in range(200):
            final = jobs.current(self.profile)
            if final['job']['state'] != 'running': break
            time.sleep(.01)
        self.assertEqual(final['job']['state'], 'failed')
        self.assertNotIn('credentials', str(final))
        while _busy(self.profile.root/'backup-jobs'/'worker.lock'): time.sleep(.005)
        for _ in range(4): self.assertTrue(_SLOTS.acquire(blocking=False))
        for _ in range(4): _SLOTS.release()

    def test_restart_preserves_intent_and_never_recaptures(self):
        from finix.backup_jobs import BackupJobs
        jobs = BackupJobs()
        root, path, _ = jobs._paths(self.profile)
        now = time.time()
        capture = dict(archive='retained.age', sha256='f'*64, complete=True, omitted_count=0)
        job = dict(id='abc', state='running', phase='upload', progress={}, started_at=now,
                   updated_at=now, error_code=None, verified=False, complete=None,
                   omitted_count=None, binding='binding', capture=capture)
        request_id = '11111111-1111-4111-8111-111111111111'
        core.atomic_json(path, dict(profile_id=self.profile.profile_id, job=job, requests={request_id: job}))
        self.assertEqual(jobs.current(self.profile)['job']['state'], 'interrupted')
        seen = []
        class Flow:
            def _binding(self): return 'binding'
            def check_and_run(self, **kwargs):
                seen.append(kwargs['resume'])
                return {'verified': True, 'first_backup': {'complete': True, 'omitted_count': 0}}
        jobs = BackupJobs(flow_factory=lambda p: Flow())
        self.assertEqual(jobs.start(self.profile, self.profile.profile_id, request_id)['job']['id'], 'abc')
        for _ in range(200):
            final = jobs.current(self.profile)
            if final['job']['state'] != 'running': break
            time.sleep(.01)
        self.assertEqual(seen, [capture])
        self.assertEqual(final['job']['state'], 'succeeded')

    def test_external_operation_is_read_only_and_cross_profile_rejected(self):
        from finix.backup_jobs import BackupJobs
        jobs = BackupJobs(flow_factory=lambda p: self.fail('must not create flow'))
        held, release = threading.Event(), threading.Event()
        def legacy():
            with core.locked(self.profile.root):
                held.set()
                release.wait(3)
        thread = threading.Thread(target=legacy)
        thread.start()
        self.assertTrue(held.wait(1))
        try:
            result = jobs.current(self.profile)
            self.assertEqual(result['job']['phase'], 'external')
            self.assertIsNone(result['job']['progress']['percent'])
            self.assertEqual(jobs.start(self.profile, self.profile.profile_id,
                '11111111-1111-4111-8111-111111111111')['job']['id'], result['job']['id'])
            with self.assertRaisesRegex(core.FinixError, 'PROFILE_CHANGED'):
                jobs.start(self.profile, 'other', '11111111-1111-4111-8111-111111111111')
            self.assertFalse((self.profile.root / 'backup-jobs' / 'state.json').exists())
        finally:
            release.set()
            thread.join(2)

    def test_file_lease_is_cross_process_and_releases_on_exit(self):
        from finix.backup_jobs import file_lease
        import subprocess, sys
        path = self.profile.root / 'lease.lock'
        code = ('import sys; from pathlib import Path; from finix.backup_jobs import file_lease; '
                '\nwith file_lease(Path(sys.argv[1])): print("held", flush=True); sys.stdin.readline()')
        process = subprocess.Popen([sys.executable, '-c', code, str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'held')
            with self.assertRaisesRegex(core.FinixError, 'JOB_BUSY'):
                with file_lease(path): self.fail('cross process lease admitted')
        finally:
            process.communicate('\n', timeout=5)
        with file_lease(path): pass

if __name__ == '__main__': unittest.main()
