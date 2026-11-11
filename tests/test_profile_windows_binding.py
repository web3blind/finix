"""Real filesystem fixtures only. No live profiles, keys or provider access."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from finix import core
from finix.integration import Profile


def digest(home):
    return hashlib.sha256(str(home).encode()).hexdigest()


class ProfileBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'Profiles' / 'MixedCaseHome'
        core.private_dir(self.home)
        self.recipient = core.keygen(Path(self.tmp.name) / 'generated-fixture.key')
        db = sqlite3.connect(self.home / 'state.db')
        db.execute('create table fixture (value text)')
        db.execute("insert into fixture values ('synthetic')")
        db.commit()
        db.close()

    def legacy(self, home, populated=True):
        root = home.parent / 'finix-data' / digest(home)
        core.private_dir(root)
        core.atomic_json(root / 'backend.json', {'profile_id': digest(home)})
        if populated:
            core.atomic_json(root / 'settings.json', {'recipient': self.recipient,
                'storage': 'local', 'retention': 3, 'interval_hours': 0,
                'next_run': 0, 'folder_id': None, 'auth_reference': 'unchanged'})
        return root

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_existing_registered_legacy_binding_survives_case_alias(self):
        legacy = self.legacy(self.home)
        before = {p.name: p.read_bytes() for p in legacy.iterdir()}
        alias = Path(str(self.home).lower())
        self.assertTrue(os.path.samefile(self.home, alias))
        self.assertNotEqual(digest(self.home), digest(alias))
        provided = Profile(self.home, default_root=self.home)
        discovered = Profile(alias, default_root=self.home)
        self.assertEqual(str(discovered.root), str(provided.root))
        self.assertEqual(discovered.profile_id, provided.profile_id)
        self.assertEqual(str(discovered.home), str(self.home))
        self.assertEqual(discovered.config()['recipient'], self.recipient)
        self.assertEqual({p.name: p.read_bytes() for p in legacy.iterdir()}, before)

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_lowercase_legacy_is_found_before_any_new_binding_is_created(self):
        alias = Path(str(self.home).lower())
        legacy = self.legacy(alias)
        profile = Profile(self.home, default_root=self.home)
        self.assertEqual(str(profile.root), str(legacy))
        self.assertEqual(str(profile.home), str(alias))
        self.assertEqual(profile.config()['auth_reference'], 'unchanged')

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_discovery_first_uses_verified_original_environment_home(self):
        from unittest.mock import patch
        original = Path(str(self.home).replace('Profiles', 'proFiles'))
        self.assertTrue(os.path.samefile(original, self.home))
        legacy = self.legacy(original)
        self.legacy(self.home, populated=False)
        with patch.dict(os.environ, {'HERMES_HOME': str(original)}):
            discovered = Profile(self.home, default_root=self.home)
        self.assertEqual(str(discovered.home), str(original))
        self.assertEqual(str(discovered.root), str(legacy))
        # Durable original spelling remains authoritative when ambient home is gone.
        with patch.dict(os.environ, {'HERMES_HOME': str(self.tmp.name)}):
            reopened = Profile(str(self.home).lower(), default_root=self.home)
        self.assertEqual(str(reopened.home), str(original))
        self.assertEqual(reopened.profile_id, digest(original))

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_backend_only_legacy_identity_is_not_rotated(self):
        alias = Path(str(self.home).lower())
        legacy = self.legacy(alias, populated=False)
        profile = Profile(self.home, default_root=self.home)
        self.assertEqual(profile.profile_id, digest(alias))
        self.assertEqual(str(profile.root), str(legacy))

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_extended_path_and_empty_discovery_do_not_hide_pending_cipher(self):
        from finix.backup_jobs import BackupJobs, _progress
        legacy = self.legacy(self.home)
        alias = Path(str(self.home).lower())
        self.legacy(alias, populated=False)
        core.private_dir(legacy / 'copies')
        cipher = legacy / 'copies' / 'retained.age'
        source_bytes = (self.home / 'state.db').read_bytes()
        with core.regular_output(cipher) as fd:
            os.write(fd, core.run_age(['-r', self.recipient], data=source_bytes))
        core.atomic_json(legacy / 'pending.json', {'source': digest(self.home),
            'namespace': 'original-namespace', 'storageFingerprint': 'same-account',
            'cipher': str(cipher), 'complete': None})
        jobroot = core.private_dir(legacy / 'backup-jobs')
        job = dict(id='11111111-1111-4111-8111-111111111111', state='interrupted',
            phase='interrupted', progress=_progress(), started_at=1, updated_at=2,
            error_code='BACKUP_INTERRUPTED', verified=False, complete=None, omitted_count=None,
            checkpoint={'path': str(cipher), 'source': digest(self.home)})
        core.atomic_json(jobroot / 'state.json', {'profile_id': digest(self.home),
            'job': job, 'requests': {}})
        before = {str(p.relative_to(legacy)): p.read_bytes() for p in legacy.rglob('*') if p.is_file()}
        profile = Profile('\\\\?\\' + str(self.home), default_root=self.home)
        self.assertEqual(str(profile.root), str(legacy))
        self.assertEqual(BackupJobs().current(profile)['job']['id'], job['id'])
        # Polling may create lease files, but every retained preexisting byte is unchanged.
        for name, value in before.items():
            self.assertEqual((legacy / name).read_bytes(), value)
        self.assertEqual(str(Profile(alias, default_root=self.home).home), str(self.home))
        self.assertEqual(core.run_age(['-d', '-i', str(Path(self.tmp.name) / 'generated-fixture.key')],
            data=cipher.read_bytes()), source_bytes)

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_two_populated_bindings_stop_without_merge(self):
        one = self.legacy(self.home)
        two = self.legacy(Path(str(self.home).lower()))
        before = {str(p): p.read_bytes() for root in (one, two) for p in root.iterdir()}
        with self.assertRaisesRegex(core.FinixError, 'PROFILE_BINDING_CONFLICT'):
            Profile(self.home, default_root=self.home)
        self.assertEqual({str(p): p.read_bytes() for root in (one, two) for p in root.iterdir()}, before)

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_concurrent_alias_initialization_converges_without_identity_rotation(self):
        import subprocess
        import sys
        code = """import sys,time,json
from finix.integration import Profile
for attempt in range(100):
    try:
        p=Profile(sys.argv[1],default_root=sys.argv[1]); print(json.dumps([str(p.home),str(p.root),p.profile_id])); break
    except OSError as e:
        if 'Another Finix operation' not in str(e): raise
        time.sleep(.01)
else: raise RuntimeError('binding initialization did not converge')
"""
        aliases = [str(self.home), str(self.home).lower(), '\\\\?\\' + str(self.home)]
        children = [subprocess.Popen([sys.executable, '-c', code, alias],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for alias in aliases]
        outputs = []
        for child in children:
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err)
            outputs.append(json.loads(out))
        self.assertTrue(all(item == outputs[0] for item in outputs))

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_active_worker_lease_keeps_legacy_binding_and_cipher_untouched(self):
        from finix.backup_jobs import file_lease
        legacy = self.legacy(self.home)
        jobs = core.private_dir(legacy / 'backup-jobs')
        with file_lease(jobs / 'worker.lock'):
            original = Profile(self.home, default_root=self.home)
            alias = Profile(str(self.home).lower(), default_root=self.home)
            self.assertEqual(original.profile_id, alias.profile_id)
            self.assertEqual(str(alias.root), str(legacy))
            self.assertTrue((legacy / 'settings.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_recorded_namespace_disappearance_stops_instead_of_recreating_identity(self):
        self.legacy(self.home)
        profile = Profile(self.home, default_root=self.home)
        moved = profile.root.with_name('parked-for-fixture')
        profile.root.rename(moved)
        with self.assertRaisesRegex(core.FinixError, 'PROFILE_BINDING_CONFLICT'):
            Profile(self.home, default_root=self.home)
        self.assertFalse(profile.root.exists())

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_case_sensitive_metadata_never_selects_a_casefolded_binding(self):
        from finix import windows_native as win
        from unittest.mock import patch
        import ctypes as C
        from ctypes import wintypes as W
        actual_api = win.api
        def api(dll, name, *args):
            if name == 'GetFileInformationByHandleEx':
                def query(handle, kind, output, size):
                    C.cast(output, C.POINTER(W.DWORD)).contents.value = 1
                    return True
                return query
            return actual_api(dll, name, *args)
        with patch.object(win, 'api', api):
            with self.assertRaisesRegex(OSError, 'Case-sensitive'):
                Profile(self.home, default_root=self.home)
        self.assertFalse((self.home.parent / 'finix-data').exists())

    @unittest.skipUnless(os.name == 'nt', 'native NTFS aliases')
    def test_native_case_sensitive_parent_fails_closed_before_binding(self):
        import subprocess
        result = subprocess.run(['fsutil.exe', 'file', 'setCaseSensitiveInfo',
            str(self.home.parent), 'enable'], capture_output=True, timeout=10)
        if result.returncode:
            self.skipTest('NTFS case-sensitive-directory flag unavailable')
        from finix import windows_native as win
        import ctypes as C
        from ctypes import wintypes as W
        query = win.api(win.K, 'GetFileInformationByHandleEx', W.BOOL,
            W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
        flags = W.DWORD()
        with win.directory(self.home.parent) as anchor:
            if not query(anchor.handle, 23, C.byref(flags), C.sizeof(flags)) or not flags.value & 1:
                self.skipTest('Windows did not enable per-directory case sensitivity')
        with self.assertRaisesRegex(OSError, 'Case-sensitive'):
            Profile(self.home, default_root=self.home)
        self.assertFalse((self.home.parent / 'finix-data').exists())

    @unittest.skipUnless(os.name != 'nt', 'POSIX identity guard')
    def test_posix_case_distinct_homes_keep_existing_hashes(self):
        other = self.home.with_name('mixedcasehome')
        core.private_dir(other)
        a, b = Profile(self.home, default_root=self.home), Profile(other, default_root=other)
        self.assertEqual(a.root.name, digest(self.home))
        self.assertEqual(b.root.name, digest(other))
        self.assertNotEqual(a.profile_id, b.profile_id)


if __name__ == '__main__':
    unittest.main()
