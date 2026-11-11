"""Native Windows legacy status scaffolding; generated, isolated fixtures only."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from finix import core
from finix.backup_jobs import BackupJobs, file_lease
from finix.integration import Profile


def digest(path):
    return hashlib.sha256(str(path).encode()).hexdigest()


@unittest.skipUnless(os.name == 'nt', 'native Windows binding admission')
class StatusLockCompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.home = core.private_dir(Path(self.tmp.name) / 'Profiles' / 'MixedHome')
        self.original = Path(str(self.home).replace('Profiles', 'proFiles'))
        self.original_root = self.legacy(self.original)
        core.atomic_json(self.original_root / 'settings.json', {'recipient': 'synthetic-public', 'storage': 'local'})
        self.shadow = self.legacy(self.home)
        # Real old current-job producer: no Profile constructor/routing record.
        status = BackupJobs().current(SimpleNamespace(root=self.shadow, profile_id=digest(self.home)))
        self.assertIsNone(status['job'])
        self.assertEqual((self.shadow / 'backup-jobs/state.lock').read_bytes(), b'0')
        self.env = patch.dict(os.environ, {'HERMES_HOME': str(self.original)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def legacy(self, home):
        root = core.private_dir(home.parent / 'finix-data' / digest(home))
        core.atomic_json(root / 'backend.json', {'profile_id': digest(home)})
        return root

    def snapshot(self):
        return {str(p): p.read_bytes() for root in (self.original_root, self.shadow)
                for p in root.rglob('*') if p.is_file()}

    def test_status_get_shadow_reopens_discovery_first_and_both_orders_without_writes(self):
        before = self.snapshot()
        for home in (self.home, self.original, self.original, self.home):
            profile = Profile(home, default_root=self.home)
            self.assertEqual(str(profile.home), str(self.original))
            self.assertEqual(str(profile.root), str(self.original_root))
            self.assertEqual(profile.profile_id, digest(self.original))
            self.assertEqual(profile.config()['recipient'], 'synthetic-public')
        self.assertEqual(before, self.snapshot())

    def test_unknown_job_state_archives_copies_and_nonempty_lock_directory_conflict(self):
        for name in ('backup-jobs/state.json', 'backup-jobs/history/old.json',
                     'copies/retained.age', 'backup-jobs/state.lock/unknown'):
            with self.subTest(name=name):
                target = self.shadow / name
                if name.startswith('backup-jobs/state.lock/'):
                    (self.shadow / 'backup-jobs/state.lock').unlink()
                core.private_dir(target.parent)
                core.atomic_json(target, {'unknown': True})
                before = self.snapshot()
                with self.assertRaisesRegex(core.FinixError, 'PROFILE_BINDING_CONFLICT'):
                    Profile(self.home, default_root=self.home)
                self.assertEqual(before, self.snapshot())
                target.unlink()
                if name.startswith('backup-jobs/state.lock/'):
                    target.parent.rmdir()
                    with core.regular_output(target.parent) as fd:
                        os.write(fd, b'0')

    def test_active_shadow_status_lock_does_not_merge(self):
        before = self.snapshot()
        with file_lease(self.shadow / 'backup-jobs/state.lock'):
            with self.assertRaisesRegex(core.FinixError, 'PROFILE_BINDING_CONFLICT'):
                Profile(self.home, default_root=self.home)
        self.assertEqual(before, self.snapshot())

    def test_active_shadow_operation_does_not_merge(self):
        import threading
        ready, release = threading.Event(), threading.Event()
        def held():
            with core.locked(self.shadow):
                ready.set()
                release.wait(5)
        worker = threading.Thread(target=held)
        worker.start()
        self.assertTrue(ready.wait(5))
        try:
            with self.assertRaises((OSError, core.FinixError)):
                Profile(self.home, default_root=self.home)
        finally:
            release.set()
            worker.join(5)

    def test_hardlink_added_after_regular_open_is_rejected(self):
        from contextlib import contextmanager
        opener = core.regular_input
        lock = self.shadow / 'backup-jobs/state.lock'
        @contextmanager
        def raced(path):
            with opener(path) as fd:
                if Path(path) == lock:
                    os.link(lock, self.home / 'late-hardlink')
                yield fd
        with patch.object(core, 'regular_input', raced):
            with self.assertRaises((OSError, core.FinixError)):
                Profile(self.home, default_root=self.home)

    def test_hardlinked_lock_is_not_ignored(self):
        lock = self.shadow / 'backup-jobs/state.lock'
        os.link(lock, self.home / 'hardlink')
        with self.assertRaises((OSError, core.FinixError)):
            Profile(self.home, default_root=self.home)

    def test_invalid_lock_bytes_and_bare_directory_remain_conflicts(self):
        lock = self.shadow / 'backup-jobs/state.lock'
        for payload in (b'', b'1', b'00', b'unknown'):
            with self.subTest(payload=payload):
                lock.unlink()
                with core.regular_output(lock) as fd:
                    os.write(fd, payload)
                with self.assertRaises((OSError, core.FinixError)):
                    Profile(self.home, default_root=self.home)
        lock.unlink()
        with self.assertRaisesRegex(core.FinixError, 'PROFILE_BINDING_CONFLICT'):
            Profile(self.home, default_root=self.home)

    def test_junction_status_directory_is_not_ignored(self):
        import subprocess
        jobs = self.shadow / 'backup-jobs'
        (jobs / 'state.lock').unlink()
        jobs.rmdir()
        target = core.private_dir(self.home / 'junction-target')
        with core.regular_output(target / 'state.lock') as fd:
            os.write(fd, b'0')
        subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(jobs), str(target)],
                       capture_output=True, check=True)
        try:
            with self.assertRaises((OSError, core.FinixError)):
                Profile(self.home, default_root=self.home)
        finally:
            jobs.rmdir()

    def test_untrusted_status_directory_is_not_ignored(self):
        import subprocess
        jobs = self.shadow / 'backup-jobs'
        subprocess.run(['icacls.exe', str(jobs), '/grant', '*S-1-1-0:(F)'], capture_output=True, check=True)
        try:
            with self.assertRaises((OSError, core.FinixError)):
                Profile(self.home, default_root=self.home)
        finally:
            subprocess.run(['icacls.exe', str(jobs), '/remove:g', '*S-1-1-0'], capture_output=True, check=True)

    def test_status_lock_and_ancestors_retained_through_publication(self):
        atomic = core.atomic_json
        observed = []
        def checked(path, value):
            if Path(path).name.startswith('home-binding-'):
                with self.assertRaises(core.FinixError):
                    with file_lease(self.shadow / 'backup-jobs/state.lock'):
                        pass
                for target in (self.shadow, self.shadow / 'backup-jobs', self.shadow / 'backup-jobs/state.lock'):
                    with self.assertRaises(OSError):
                        target.rename(target.with_name(target.name + '-moved'))
                observed.append(True)
            return atomic(path, value)
        with patch.object(core, 'atomic_json', checked):
            Profile(self.home, default_root=self.home)
        self.assertEqual(observed, [True])
