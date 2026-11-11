"""Local-only active-source contracts; never touches a real profile."""
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from finix import core, streaming

RUNTIME = ('cron/.tick.lock', 'cron/ticker_heartbeat', 'cron/ticker_last_success',
           'state/dashboard_clients.heartbeat', 'state/gateway.heartbeat',
           'gateway_state.json', 'runtime/active_sessions.json', 'channel_directory.json',
           'auth.lock', 'shared/nous_auth.lock')

class RuntimePolicyTests(unittest.TestCase):
    def test_sqlite_validation_diagnostic_is_closed_and_path_free(self):
        from finix.setup_errors import setup_error_code
        self.assertEqual(setup_error_code(core.FinixError('SQLite source validation failed')),
                         'SETUP_SQLITE_FAILED')
        self.assertEqual(setup_error_code(core.FinixError('SQLite source validation failed: PRIVATE_PATH')),
                         'SETUP_CHECK_FAILED')

    def test_exact_regenerated_runtime_paths_only(self):
        for name in RUNTIME:
            with self.subTest(name=name):
                self.assertTrue(core.excluded(name))
                self.assertFalse(core.allowed(name))
                for near in (name + '.user', 'project/' + name, name + '/user-data'):
                    self.assertFalse(core.excluded(near), near)
        for name in ('auth.json', 'shared/nous_auth.json', 'channel_aliases.json',
                     'owner.lock', 'cron/jobs.json', 'state/user.heartbeat', 'tools/age.exe'):
            self.assertTrue(core.allowed(name))
        if os.name == 'nt':
            self.assertTrue(core.excluded('CRON/.TICK.LOCK'))

class LiveSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = core.private_dir(self.base / 'profile')
        self.stage = core.private_dir(self.home / 'cache' / 'finix' / 'sqlite-temp')

    def test_sqlite_companion_swap_while_reader_open_fails(self):
        from contextlib import closing
        with closing(sqlite3.connect(self.home / 'live.db')) as db:
            db.execute('pragma journal_mode=wal')
            db.execute('create table data(value)')
            db.commit()
            with core.locked(self.home) as anchor:
                inventory = core.scan(anchor)
                with self.assertRaises((core.FinixError, PermissionError)):
                    with core.source_file(anchor, 'live.db', inventory['live.db']):
                        wal = self.home / 'live.db-wal'
                        wal.rename(self.home / 'saved-wal')
                        wal.write_bytes(b'not the pinned WAL')

    def test_scan_keeps_tuple_fingerprints_with_validated_sqlite_provenance(self):
        from contextlib import closing
        path = self.home / 'live.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table data(value)')
            db.commit()
        ordinary = self.home / 'ordinary.txt'
        ordinary.write_bytes(b'owner content')
        with core.locked(self.home) as anchor:
            inventory = core.scan(anchor)
            for name, record in inventory.items():
                self.assertIsInstance(record, tuple)
                self.assertEqual(len(record), 6)
                with (self.home / name).open('rb') as source:
                    self.assertEqual(record, core.fingerprint(os.fstat(source.fileno())))
            self.assertTrue(inventory['live.db'].sqlite)
            self.assertFalse(inventory['ordinary.txt'].sqlite)
            legacy = tuple(inventory['live.db'])
            with closing(sqlite3.connect(path)) as db:
                db.execute('insert into data values (zeroblob(65536))')
                db.commit()
            with self.assertRaises(core.FinixError):
                with core.source_file(anchor, 'live.db', legacy):
                    self.fail('legacy tuple silently granted SQLite relaxation')
            with core.source_file(anchor, 'live.db', inventory['live.db']):
                pass  # Validated provenance permits legitimate committed writes.

    def test_companions_may_regenerate_after_completed_database_snapshot(self):
        path = self.home / 'live.db'
        db = sqlite3.connect(path)
        try:
            db.execute('pragma journal_mode=wal')
            db.execute('create table data(value)')
            db.execute('insert into data values (42)')
            db.commit()
            with core.locked(self.home) as anchor:
                inventory = core.scan(anchor)
                with core.source_file(anchor, 'live.db', inventory['live.db']) as (fd, parent, name):
                    snapshot = core.sqlite_snapshot(fd, parent, name)
                    self.assertTrue(snapshot)
                # The last connection closes AFTER the snapshot/held-reader scope.
                # SQLite may checkpoint and delete/recreate its coordination files.
                db.close()
                db = sqlite3.connect(path)
                db.execute('pragma journal_mode=wal')
                db.execute('insert into data values (43)')
                db.commit()
                final = core.scan(anchor)
                companions = {'live.db-wal', 'live.db-shm', 'live.db-journal'}
                core.check_inventory(inventory, final, companions, {'live.db'}, anchor)
        finally:
            db.close()

    def test_new_sqlite_generation_requires_real_validation_and_snapshot(self):
        from contextlib import closing
        from unittest.mock import patch
        template = self.base / 'template.db'
        with closing(sqlite3.connect(template)) as db:
            db.execute('create table data(value)')
            db.execute('insert into data values (42)')
            db.commit()
        replacement = template.read_bytes()
        path = self.home / 'ordinary.txt'
        original_scan = core.scan
        for mode in ('source', 'buffered', 'streaming'):
            with self.subTest(mode=mode):
                path.write_bytes(b'original plain text')
                calls = []
                def scan(*args, **kwargs):
                    result = original_scan(*args, **kwargs)
                    if not calls:
                        identity = core.fingerprint(path.stat())[:3]
                        # Truncate/write the SAME file, not a rename/replacement.
                        with path.open('r+b') as target:
                            target.write(replacement)
                            target.truncate()
                        self.assertEqual(core.fingerprint(path.stat())[:3], identity)
                    calls.append(1)
                    return result
                with core.locked(self.home) as anchor, patch.object(core, 'scan', scan):
                    if mode == 'source':
                        expected = core.scan(anchor)['ordinary.txt']
                        with streaming.captured_source(anchor, 'ordinary.txt', expected) as (_, _, _, captured):
                            self.assertTrue(captured.sqlite)
                    elif mode == 'buffered':
                        files = core.validate_archive(core.make_archive(anchor))
                        self.assertTrue(files['ordinary.txt'].startswith(b'SQLite format 3\x00'))
                    else:
                        output = io.BytesIO()
                        streaming.write_archive(anchor, output, self.stage)
                        self.assertTrue(core.validate_archive(output.getvalue())['ordinary.txt'].startswith(b'SQLite format 3\x00'))

    def test_ordinary_content_changes_before_capture_are_allowed(self):
        path = self.home / 'owner.lock'
        path.write_bytes(b'user content')
        with core.locked(self.home) as anchor:
            expected = core.scan(anchor)['owner.lock']
            path.write_bytes(b'changed content')
            with core.source_file(anchor, 'owner.lock', expected) as (fd, parent, leaf):
                with streaming.ordinary_payload(fd, parent, leaf) as (stream, size):
                    self.assertEqual(stream.read(), b'changed content')

    def test_replaced_database_and_fake_header_fail(self):
        from contextlib import closing
        for attack in ('replace', 'header', 'fake'):
            with self.subTest(attack=attack):
                path = self.home / 'live.db'
                if path.exists():
                    path.unlink()
                with closing(sqlite3.connect(path)) as db:
                    db.execute('create table data(value)')
                    db.commit()
                with core.locked(self.home) as anchor:
                    expected = core.scan(anchor)['live.db']
                    if attack == 'replace':
                        path.rename(self.home / 'old.db')
                        with closing(sqlite3.connect(path)) as replacement:
                            replacement.execute('create table other(value)')
                            replacement.commit()
                    elif attack == 'header':
                        path.write_bytes(b'wrong header')
                    else:
                        path.write_bytes(b'SQLite format 3\x00' + bytes(8192))
                    with self.assertRaises(core.FinixError):
                        with core.source_file(anchor, 'live.db', expected):
                            self.fail('invalid database accepted')

    def test_runtime_records_omitted_but_near_matches_and_tools_roundtrip(self):
        recipient = core.keygen(self.base / 'synthetic-identity')
        for name in RUNTIME:
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'regenerated')
            path.with_name(path.name + '.user').write_bytes(b'owner data')
        (self.home / 'tools').mkdir()
        (self.home / 'tools' / 'keep').write_bytes(b'installed tool')
        result = core.backup(self.home, self.base / 'out', self.base / 'state', recipient)
        restored = self.base / 'restored'
        core.restore(result['path'], self.base / 'synthetic-identity', restored)
        for name in RUNTIME:
            self.assertFalse((restored / name).exists())
            self.assertEqual((restored / (name + '.user')).read_bytes(), b'owner data')
        self.assertEqual((restored / 'tools' / 'keep').read_bytes(), b'installed tool')

    def test_multiroot_accepts_later_sqlite_and_ordinary_generations(self):
        from contextlib import closing
        extra = core.private_dir(self.base / 'extra')
        (extra / 'later').write_bytes(b'other root')
        ordinary = self.home / 'ordinary'
        ordinary.write_bytes(b'owner data')
        with closing(sqlite3.connect(self.home / 'live.db')) as db:
            db.execute('pragma journal_mode=wal')
            db.execute('create table data(value)')
            db.execute('insert into data values (1)')
            db.commit()
            original = streaming._add_payload
            from unittest.mock import patch
            for mutate_user in (False, True):
                def add(*args, **kwargs):
                    if args[1] == 'external/projects/later':
                        db.execute('insert into data values (2)')
                        db.commit()
                        db.execute('pragma wal_checkpoint(full)')
                        if mutate_user:
                            ordinary.write_bytes(b'changed owner data')
                    return original(*args, **kwargs)
                output = io.BytesIO()
                with core.locked(self.home) as main, core.locked(extra) as other, patch.object(streaming, '_add_payload', add):
                    streaming.write_archive(main, output, self.stage,
                        extra_fds=[({'name': 'projects', 'path': str(extra)}, other)])
                    files = core.validate_archive(output.getvalue())
                    self.assertEqual(set(files), {'profile/ordinary', 'profile/live.db', 'external/projects/later'})
                    self.assertEqual(files['profile/ordinary'], b'owner data')

    def test_wrong_header_after_payload_fails_final_inventory(self):
        from contextlib import closing
        from unittest.mock import patch
        path = self.home / 'live.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table data(value)')
            db.commit()
        original = streaming._add_payload
        def add(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1] == 'live.db':
                path.write_bytes(b'not sqlite anymore')
            return result
        with core.locked(self.home) as anchor, patch.object(streaming, '_add_payload', add):
            with self.assertRaises(core.FinixError):
                streaming.write_archive(anchor, io.BytesIO(), sqlite_temp_dir=self.stage)

    def test_buffered_sqlite_allows_checkpoint_after_inventory(self):
        from contextlib import closing
        from unittest.mock import patch
        with closing(sqlite3.connect(self.home / 'live.db')) as db:
            db.execute('create table data(value)')
            db.commit()
            original = core.scan
            calls = []
            def scan(*args, **kwargs):
                result = original(*args, **kwargs)
                if not calls:
                    db.execute('insert into data values (zeroblob(65536))')
                    db.commit()
                calls.append(1)
                return result
            with core.locked(self.home) as anchor, patch.object(core, 'scan', scan):
                archive = core.make_archive(anchor)
            self.assertEqual(set(core.validate_archive(archive)), {'live.db'})

    def test_wal_commits_after_discovery_and_during_snapshot_restore(self):
        from contextlib import closing
        from unittest.mock import patch
        import threading
        recipient = core.keygen(self.base / 'synthetic-identity')
        with closing(sqlite3.connect(self.home / 'live.db')) as db:
            db.execute('pragma journal_mode=wal')
            db.execute('pragma wal_autocheckpoint=0')
            db.execute('create table data(value integer)')
            db.execute('insert into data values (1)')
            db.commit()
            original_scan = core.scan
            calls = []
            def scan(*args, **kwargs):
                result = original_scan(*args, **kwargs)
                if not calls:
                    db.execute('insert into data values (2)')
                    db.commit()
                    db.execute('pragma wal_checkpoint(full)')
                calls.append(1)
                return result
            committed = threading.Event()
            def write():
                with closing(sqlite3.connect(self.home / 'live.db')) as writer:
                    writer.execute('insert into data values (3)')
                    writer.commit()
                    committed.set()
            threads = []
            def progress(record):
                if record['phase'] == 'sqlite' and not threads:
                    thread = threading.Thread(target=write)
                    threads.append(thread)
                    thread.start()
                    thread.join(5)
                    self.assertTrue(committed.is_set(), 'writer blocked by backup')
            with patch.object(core, 'scan', scan):
                result = core.backup(self.home, self.base / 'out', self.base / 'state', recipient,
                                     sqlite_temp_dir=self.stage, progress=progress)
            self.assertTrue(committed.is_set())
            restored = self.base / 'restored'
            core.restore(Path(result['path']), self.base / 'synthetic-identity', restored)
            with closing(sqlite3.connect(restored / 'live.db')) as recovered:
                self.assertEqual(recovered.execute('pragma integrity_check').fetchone(), ('ok',))
                self.assertEqual(recovered.execute('select * from data').fetchall(), [(1,), (2,)])
            archived = core.decrypt(Path(result['path']), self.base / 'synthetic-identity')
            files = core.validate_archive(archived)
            self.assertNotIn('live.db-wal', files)

if __name__ == '__main__':
    unittest.main()
