import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import hashlib
import gzip
import fcntl
import sys

from finix import core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'profile'
        self.source.mkdir()
        (self.source / 'config.yaml').write_text('model: synthetic\n')
        (self.source / '.env').write_text('SYNTHETIC_TOKEN=fixture-only\n')
        (self.source / 'memories').mkdir()
        (self.source / 'memories/MEMORY.md').write_text('synthetic memory')
        (self.source / 'skills/demo').mkdir(parents=True)
        (self.source / 'skills/demo/SKILL.md').write_text('# Demo')
        (self.source / 'cron').mkdir()
        (self.source / 'cron/jobs.json').write_text(json.dumps({'jobs': [
            {'id': 'a', 'enabled': True, 'state': 'scheduled', 'next_run_at': '2099-01-01'},
            {'id': 'b', 'state': 'scheduled'}]}))
        (self.source / 'logs').mkdir()
        (self.source / 'logs/secret.log').write_text('excluded')
        (self.source / 'unknown').write_text('excluded')
        self.db = sqlite3.connect(self.source / 'state.db')
        self.db.execute('pragma journal_mode=WAL')
        self.db.execute('create table fixture(value text)')
        self.db.execute("insert into fixture values ('persisted')")
        self.db.commit()
        self.addCleanup(self.db.close)
        self.key = self.root / 'recovery.key'
        self.recipient = core.keygen(self.key)
        self.store = self.root / 'backups'
        self.state = self.root / 'state'

    def backup(self, **kw):
        return core.backup(self.source, self.store, self.state, self.recipient, **kw)

    def test_roundtrip_and_permissions(self):
        result = self.backup()
        target = self.root / 'restore'
        core.restore(result['path'], self.key, target)
        self.assertEqual((target / '.env').read_bytes(), (self.source / '.env').read_bytes())
        self.assertEqual((target / 'unknown').read_bytes(), (self.source / 'unknown').read_bytes())
        self.assertFalse((target / 'logs').exists())
        self.assertFalse((target / 'state.db-wal').exists())
        conn = sqlite3.connect(target / 'state.db')
        try:
            self.assertEqual(conn.execute('select value from fixture').fetchone()[0], 'persisted')
        finally:
            conn.close()
        for job in json.loads((target / 'cron/jobs.json').read_text())['jobs']:
            self.assertIs(job['enabled'], False)
            self.assertEqual(job['state'], 'paused')
            self.assertIsNone(job['next_run_at'])
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual((target / '.env').stat().st_mode & 0o777, 0o600)
        for path in [*self.store.rglob('*'), *self.state.rglob('*')]:
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(b'SYNTHETIC_TOKEN', data)
                self.assertNotIn(b'AGE-SECRET-KEY-', data)

    def test_wrong_key_and_corruption_leave_target_unchanged(self):
        result = self.backup()
        wrong = self.root / 'wrong.key'
        core.keygen(wrong)
        target = self.root / 'restore'
        target.mkdir()
        with self.assertRaises(core.FinixError):
            core.restore(result['path'], wrong, target)
        self.assertEqual(list(target.iterdir()), [])
        path = Path(result['path'])
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        with self.assertRaises(core.FinixError):
            core.restore(path, self.key, target)
        self.assertEqual(list(target.iterdir()), [])

    def test_no_overwrite(self):
        result = self.backup()
        with self.assertRaises(core.FinixError):
            core.restore(result['path'], self.key, self.source)
        self.assertTrue((self.source / '.env').exists())
        with self.assertRaises(core.FinixError):
            core.keygen(self.key)

    def test_source_symlink_rejected(self):
        (self.source / 'memories/link').symlink_to(self.root / 'outside')
        with self.assertRaises(core.FinixError):
            self.backup()
        self.assertFalse(list(self.store.glob('*.age')) if self.store.exists() else [])

    def test_containment_rejected(self):
        with self.assertRaises(core.FinixError):
            core.backup(self.source, self.source / 'output', self.state, self.recipient)
        with self.assertRaises(core.FinixError):
            core.backup(self.source, self.store, self.source / 'state', self.recipient)

    def test_retention_is_scoped_and_failure_safe(self):
        first = self.backup(keep=1)
        foreign = self.store / 'foreign.age'
        foreign.write_bytes(b'not owned')
        with self.assertRaises(core.FinixError):
            core.backup(self.source, self.store, self.state, 'invalid', keep=1)
        self.assertTrue(Path(first['path']).exists())
        second = self.backup(keep=1)
        self.assertFalse(Path(first['path']).exists())
        self.assertTrue(Path(second['path']).exists())
        self.assertTrue(foreign.exists())

    def test_malicious_tar_paths_and_links(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('memories/link', tarfile.SYMTYPE), ('memories/link', tarfile.LNKTYPE),
                           ('unknown', tarfile.REGTYPE)]:
            with self.subTest(name=name, kind=kind):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = 'config.yaml' if kind != tarfile.REGTYPE else ''
                    archive.addfile(info)
                encrypted = self.root / 'malicious.age'
                proc = subprocess.run(['age', '-r', self.recipient], input=stream.getvalue(),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                encrypted.write_bytes(proc.stdout)
                target = self.root / 'malicious-target'
                with self.assertRaises(core.FinixError):
                    core.restore(encrypted, self.key, target)
                self.assertFalse(target.exists())

    def encrypted_fixture(self, entries, mutate_manifest=None):
        manifest = {'format': 'finix-1', 'files': {
            name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for name, data in entries.items()}}
        if mutate_manifest:
            mutate_manifest(manifest)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, data in [('manifest.json', json.dumps(manifest).encode()), *entries.items()]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        path = self.root / 'fixture.age'
        path.write_bytes(subprocess.run(['age', '-r', self.recipient], input=stream.getvalue(),
                         capture_output=True, check=True).stdout)
        return path

    def test_manifest_corruption_and_bad_cron(self):
        for entries, mutation in [
            ({'config.yaml': b'fixture'}, lambda m: m['files']['config.yaml'].update(size=0)),
            ({'cron/jobs.json': b'[]'}, None),
            ({'cron/jobs.json': b'{"jobs":[null]}'}, None),
        ]:
            archive = self.encrypted_fixture(entries, mutation)
            target = self.root / 'bad-target'
            with self.assertRaises(core.FinixError):
                core.restore(archive, self.key, target)
            self.assertFalse(target.exists())

    def test_expansion_and_file_limits(self):
        compressed = gzip.compress(b' ' * 32768)
        with mock.patch.object(core, 'MAX_BYTES', 4096):
            with self.assertRaises(core.FinixError):
                core.validate_archive(compressed)
        with mock.patch.object(core, 'MAX_FILES', 1):
            with self.assertRaises(core.FinixError):
                self.backup()
        with mock.patch.object(core, 'MAX_BYTES', 32):
            with self.assertRaises(core.FinixError):
                self.backup()
        self.assertEqual(list(self.store.glob('*.age')), [])

    def test_cooperative_source_lock(self):
        fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(core.FinixError):
                self.backup()
        finally:
            os.close(fd)

    def test_source_file_race_rejected(self):
        original = core.read_regular
        changed = False
        def racing(fd, limit=core.MAX_BYTES):
            nonlocal changed
            data = original(fd, limit)
            if not changed:
                changed = True
                (self.source / 'config.yaml').write_text('changed while backing up')
            return data
        with mock.patch.object(core, 'read_regular', side_effect=racing):
            with self.assertRaises(core.FinixError):
                self.backup()
        self.assertEqual(list(self.store.glob('*.age')), [])

    def test_fifo_and_sqlite_companion_symlink_rejected(self):
        fifo = self.source / 'memories/fifo'
        os.mkfifo(fifo)
        with self.assertRaises(core.FinixError):
            self.backup()
        fifo.unlink()
        # A journal symlink is rejected before SQLite sees it.
        (self.source / 'state.db-journal').symlink_to(self.root / 'outside')
        with self.assertRaises(core.FinixError):
            self.backup()

    def test_nested_directory_permissions(self):
        result = self.backup()
        target = self.root / 'restored'
        core.restore(result['path'], self.key, target)
        for path in target.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_state_binding_and_retention_checksum(self):
        first = self.backup()
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(core.FinixError):
            core.backup(other, self.store, self.state, self.recipient)
        Path(first['path']).write_bytes(b'changed')
        with self.assertRaises(core.FinixError):
            self.backup(keep=1)
        self.assertTrue(Path(first['path']).exists())
        self.assertFalse(core.status(self.store, self.state)['backups'][0]['verified'])

    def test_failed_readback_does_not_prune(self):
        first = self.backup()
        original = core.digest_file
        def mismatched(path):
            actual = original(path)
            return '0' * 64 if Path(path).suffix == '.age' else actual
        with mock.patch.object(core, 'digest_file', side_effect=mismatched):
            with self.assertRaises(core.FinixError):
                self.backup(keep=1)
        self.assertTrue(Path(first['path']).exists())
        self.assertEqual(len(core.status(self.store, self.state)['backups']), 1)

    def test_plugin_identity_rejected_without_executing_age(self):
        path = self.backup()['path']
        identity = self.root / 'plugin.key'
        identity.write_text('AGE-PLUGIN-TEST-1SYNTHETIC')
        with mock.patch.object(core.subprocess, 'Popen') as popen:
            with self.assertRaises(core.FinixError):
                core.restore(path, identity, self.root / 'restore')
            popen.assert_not_called()

    def test_full_cli_roundtrip(self):
        def cli(*args):
            proc = subprocess.run([sys.executable, '-m', 'finix', *args],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn('AGE-SECRET-KEY-', proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        key = self.root / 'cli.key'
        recipient = cli('keygen', '--identity', str(key))['recipient']
        result = cli('backup', '--source', str(self.source), '--output', str(self.store),
                     '--state', str(self.state), '--recipient', recipient, '--keep', '2')
        target = self.root / 'cli-restored'
        cli('restore', '--archive', result['path'], '--identity', str(key), '--target', str(target))
        self.assertEqual((target / '.env').read_bytes(), (self.source / '.env').read_bytes())
        self.assertTrue(cli('status', '--output', str(self.store), '--state', str(self.state))['backups'][0]['verified'])

    def test_age_failure_preserves_previous_backups(self):
        first = self.backup()
        with mock.patch.object(core, 'run_age', side_effect=core.FinixError('synthetic failure')):
            with self.assertRaises(core.FinixError):
                self.backup(keep=1)
        self.assertTrue(Path(first['path']).exists())
        self.assertEqual(len(list(self.store.iterdir())), 1)
        self.assertEqual(len(core.status(self.store, self.state)['backups']), 1)

    def test_duplicate_tar_entries_rejected(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for _ in range(2):
                info = tarfile.TarInfo('config.yaml')
                archive.addfile(info)
        with self.assertRaises(core.FinixError):
            core.validate_archive(stream.getvalue())

    def test_restore_target_symlink_rejected(self):
        result = self.backup()
        outside = self.root / 'outside'
        outside.mkdir()
        target = self.root / 'target-link'
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.FinixError):
            core.restore(result['path'], self.key, target)
        self.assertTrue(target.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_cli(self):
        proc = subprocess.run(['python', '-m', 'finix', 'status', '--output', str(self.store),
                               '--state', str(self.state)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['backups'], [])


if __name__ == '__main__':
    unittest.main()
