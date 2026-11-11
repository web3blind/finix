"""No-network real age/WAL acceptance for excluded in-profile SQLite staging."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from finix import core, streaming
from finix.integration import Profile


class ProfileTempTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = core.private_dir(self.base / 'profile')
        self.stage = core.private_dir(self.home / ('CaChE' if os.name == 'nt' else 'cache') / 'finix' / 'sqlite-temp')

    def test_excluded_cache_wal_encrypted_roundtrip_and_manifest(self):
        identity = self.base / 'identity'
        recipient = core.keygen(identity)
        sentinel = self.stage / 'crash-leftover.txt'
        sentinel.write_text('temporary plaintext must never enter backup')
        with closing(sqlite3.connect(self.home / 'live.db')) as db:
            db.execute('pragma journal_mode=wal')
            db.execute('pragma wal_autocheckpoint=0')
            db.execute('create table messages(value text)')
            db.execute("insert into messages values ('committed only in WAL')")
            db.commit()
            self.assertTrue((self.home / 'live.db-wal').exists())
            snapshots = []
            original = streaming.sqlite_stage
            from contextlib import contextmanager
            @contextmanager
            def observe(path, required):
                with original(path, required) as stage:
                    # Linux uses its pinned /proc/self/fd parent by design.
                    self.assertTrue(stage.resolve().is_relative_to(self.stage.resolve()))
                    if os.name == 'nt':
                        from finix.windows_native import trusted_directory
                        self.assertTrue(trusted_directory(stage, private=True))
                    else:
                        self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
                    snapshots.append(stage)
                    yield stage
            with patch.object(streaming, 'sqlite_stage', observe):
                result = core.backup(self.home, self.base / 'output', self.base / 'state', recipient,
                                     sqlite_temp_dir=self.stage)
            self.assertTrue(snapshots)
            self.assertEqual(list(self.stage.iterdir()), [sentinel])
            raw = core.decrypt(Path(result['path']), identity)
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
                manifest = json.load(archive.extractfile('manifest.json'))
                self.assertEqual(set(manifest['files']), {'live.db'})
                self.assertEqual(set(archive.getnames()), {'profile/live.db', 'manifest.json'})
                self.assertNotIn(b'temporary plaintext must never enter backup', raw)
            restored = self.base / 'restored'
            core.restore(Path(result['path']), identity, restored)
            self.assertFalse((restored / self.stage.relative_to(self.home)).exists())
            with closing(sqlite3.connect(restored / 'live.db')) as recovered:
                self.assertEqual(recovered.execute('select * from messages').fetchall(), [('committed only in WAL',)])
                self.assertEqual(recovered.execute('pragma integrity_check').fetchone(), ('ok',))

    def test_profile_config_accepts_excluded_cache_only(self):
        profile = Profile(self.home)
        result = profile.configure('sqlite_temp_dir', str(self.stage))
        self.assertEqual(result['sqlite_temp_dir'], str(self.stage))
        self.assertTrue(result['sqlite_temp']['ready'])
        for directory in (self.home, self.base, core.private_dir(self.home / 'data'),
                          core.private_dir(self.home / 'project' / 'cache')):
            with self.subTest(directory=directory), self.assertRaises(core.FinixError):
                profile.configure('sqlite_temp_dir', str(directory))

    def test_exclusion_policy_is_authoritative(self):
        recipient = core.keygen(self.base / 'identity')
        (self.home / 'owner.log').write_text('keep this file')
        with patch.object(core, 'excluded', return_value=False):
            with self.assertRaisesRegex(core.FinixError, '^NEED_SQLITE_TEMP_DIRECTORY$'):
                core.backup(self.home, self.base / 'output', self.base / 'state', recipient,
                            sqlite_temp_dir=self.stage)
        self.assertFalse((self.base / 'output').exists())

    def test_storage_and_extra_roots_disjoint_in_both_directions(self):
        for root in (self.stage, self.stage / 'child', self.stage.parent):
            with self.subTest(root=root), self.assertRaisesRegex(core.FinixError, '^NEED_SQLITE_TEMP_DIRECTORY$'):
                core.validate_sqlite_temp_location(self.stage, self.home, root)

    def test_in_profile_temp_cleans_up_on_error_and_cancel(self):
        recipient = core.keygen(self.base / 'identity')
        with closing(sqlite3.connect(self.home / 'data.db')) as db:
            db.execute('create table data(value)')
            db.execute('insert into data values (zeroblob(1048576))')
            db.commit()
        from threading import Event
        for cancel in (False, True):
            event = Event()
            observed = []
            def progress(record):
                if record['phase'] == 'sqlite':
                    observed.extend(self.stage.iterdir())
                    if cancel:
                        event.set()
                    else:
                        raise core.FinixError('synthetic snapshot failure')
            with self.subTest(cancel=cancel), self.assertRaises(core.FinixError):
                core.backup(self.home, self.base / 'output', self.base / 'state', recipient,
                    sqlite_temp_dir=self.stage, progress=progress, cancel=event)
            self.assertTrue(observed)
            self.assertEqual(list(self.stage.iterdir()), [])
            self.assertEqual(list((self.base / 'output').iterdir()), [])
            self.assertFalse((self.base / 'state' / 'index.json').exists())

    def test_stage_replaced_with_link_after_preflight_fails_closed(self):
        import subprocess
        recipient = core.keygen(self.base / 'identity')
        with closing(sqlite3.connect(self.home / 'data.db')) as db:
            db.execute('create table data(value)')
            db.commit()
        target = core.private_dir(self.home / 'included-data')
        original = streaming.write_encrypted_snapshot
        def replace(*args, **kwargs):
            self.stage.rename(self.stage.with_name('old-stage'))
            if os.name == 'nt':
                subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.stage), str(target)],
                               capture_output=True, check=True)
            else:
                self.stage.symlink_to(target, target_is_directory=True)
            return original(*args, **kwargs)
        with patch.object(streaming, 'write_encrypted_snapshot', replace):
            with self.assertRaises(core.FinixError):
                core.backup(self.home, self.base / 'output', self.base / 'state', recipient,
                            sqlite_temp_dir=self.stage)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list((self.base / 'output').iterdir()), [])

    def test_invalid_temp_location_has_closed_setup_diagnostic(self):
        from finix.setup_errors import setup_error_code
        try:
            core.validate_sqlite_temp_location(self.home, self.home)
        except core.FinixError as error:
            self.assertEqual(setup_error_code(error), 'SETUP_SQLITE_TEMP_REQUIRED')
        else:
            self.fail('profile root admitted as temporary storage')

    def test_unexcluded_source_temp_rejected_before_output(self):
        stage = core.private_dir(self.home / 'project' / 'cache')
        recipient = core.keygen(self.base / 'identity')
        with self.assertRaises(core.FinixError):
            core.backup(self.home, self.base / 'output', self.base / 'state', recipient, sqlite_temp_dir=stage)
        self.assertFalse((self.base / 'output').exists())


if __name__ == '__main__':
    unittest.main()
