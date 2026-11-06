"""Whole-profile contracts exercised only with disposable data and real age."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

from finix import core, streaming


class ProfileScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'profile'
        self.source.mkdir()
        self.output, self.state = self.root / 'output', self.root / 'state'
        self.key = self.root / 'identity'
        self.recipient = core.keygen(self.key)

    def put(self, name, data=b'fixture'):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def backup(self):
        return core.backup(self.source, self.output, self.state, self.recipient, keep=1)

    def fixture(self, entries, version='finix-2', extra=()):
        manifest = {'format': version, 'files': {
            n: {'size': len(d), 'sha256': hashlib.sha256(d).hexdigest()}
            for n, d in entries.items()}}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz', format=tarfile.USTAR_FORMAT) as tar:
            records = [('manifest.json', json.dumps(manifest).encode())]
            records += [(('profile/' if version == 'finix-2' else '') + n, d)
                        for n, d in entries.items()]
            for n, d in records:
                info = tarfile.TarInfo(n)
                info.size = len(d)
                tar.addfile(info, io.BytesIO(d))
            for info in extra:
                tar.addfile(info)
        return buffer.getvalue()

    def test_full_profile_real_age_roundtrip_with_unknown_wal_and_manifest(self):
        names = ['arbitrary.bin', 'manifest.json', 'owner.log',
                 'ai-projects/demo/.git/HEAD', 'ai-projects/demo/src/main.custom',
                 'unknown-plugin/nested/store.settings', 'plugin-data/x/.git/HEAD',
                 'plugins/x/.git/HEAD', 'unknown/tmp/valuable', 'unknown/logs/valuable']
        for name in names:
            self.put(name)
        self.put('cron/jobs.json', b'{"jobs":[{"id":"fixture","enabled":true}]}')
        outside = self.root / 'ai-projects/outside'
        outside.parent.mkdir()
        outside.write_bytes(b'outside')
        dbpath = self.put('ai-projects/demo/unknown/plugin-store.unusual', b'')
        db = sqlite3.connect(dbpath)
        self.addCleanup(db.close)
        db.execute('pragma journal_mode=WAL')
        db.execute('create table fixture(value text)')
        db.execute('insert into fixture values (?)', ('committed WAL',))
        db.commit()
        self.assertGreater(Path(str(dbpath) + '-wal').stat().st_size, 0)
        self.put('unknown/orphan-wal')
        result = self.backup()
        archive = core.decrypt(result['path'], self.key)
        entries = core.validate_archive(archive)
        target = self.root / 'restored'
        core.restore(result['path'], self.key, target)
        for name in names + ['unknown/orphan-wal']:
            self.assertEqual((target / name).read_bytes(), b'fixture', name)
        self.assertFalse((target / 'ai-projects/outside').exists())
        for suffix in ('-wal', '-shm', '-journal'):
            self.assertNotIn(str(dbpath.relative_to(self.source)) + suffix, entries)
        with closing(sqlite3.connect(target / dbpath.relative_to(self.source))) as restored:
            self.assertEqual(restored.execute('select value from fixture').fetchall(), [('committed WAL',)])
        self.assertFalse(json.loads((target / 'cron/jobs.json').read_bytes())['jobs'][0]['enabled'])
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        for folder in (self.output, self.state):
            for path in folder.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'committed WAL', path.read_bytes())

    def test_cloud_failure_keeps_old_and_new_full_profile_ciphertext(self):
        from finix.integration import Profile
        self.put('ai-projects/demo/valuable.custom')
        profile = Profile(self.source)
        profile.recipient(profile.profile_id, self.recipient, None)
        old = profile.backup()
        cfg = profile.config()
        cfg.update(storage='workspace', retention=1)
        core.atomic_json(profile.root / 'settings.json', cfg)
        transport = mock.Mock()
        transport.backup_cipher.side_effect = core.FinixError('synthetic cloud failure')
        with mock.patch.object(profile, 'workspace', return_value=transport):
            with self.assertRaises(core.FinixError):
                profile.backup()
        self.assertTrue(Path(old['path']).exists())
        copies = list((profile.root / 'copies').glob('*.age'))
        self.assertEqual(len(copies), 2)
        for index, archive in enumerate(copies):
            target = self.root / f'cloud-failure-restored-{index}'
            core.restore(archive, self.key, target)
            self.assertEqual((target / 'ai-projects/demo/valuable.custom').read_bytes(), b'fixture')

    def test_sqlite_reader_shm_changes_do_not_invalidate_snapshot(self):
        path = self.put('unknown/store', b'')
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute('pragma journal_mode=WAL')
        db.execute('create table fixture(value text)')
        db.commit()
        snapshot = streaming._sqlite_backup
        def changed_shm(*args, **kwargs):
            data = snapshot(*args, **kwargs)
            shm = Path(str(path) + '-shm')
            before = shm.stat()
            os.utime(shm, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
            return data
        with mock.patch.object(streaming, '_sqlite_backup', side_effect=changed_shm):
            result = self.backup()
        entries = core.validate_archive(core.decrypt(result['path'], self.key))
        self.assertEqual(set(entries), {'unknown/store'})

    def test_main_shared_and_named_profile_isolation_with_default_launch_env(self):
        from finix.integration import Profile
        self.put('shared/nous/fixture.json')
        self.put('config.yaml')
        self.put('profiles/named/own.custom')
        self.put('profiles/other/foreign.custom')
        external = self.root / 'external-shared'
        external.mkdir()
        (external / 'must-not-follow').write_bytes(b'outside')
        with mock.patch.dict(os.environ, {'HERMES_HOME': str(self.source),
                                         'HERMES_SHARED_AUTH_DIR': str(external)}):
            main = Profile(self.source)
            named = Profile(self.source / 'profiles/named')
            for profile in (main, named):
                profile.recipient(profile.profile_id, self.recipient, None)
            main_result = main.backup()
            named_result = named.backup()
        main_entries = core.validate_archive(core.decrypt(main_result['path'], self.key))
        named_entries = core.validate_archive(core.decrypt(named_result['path'], self.key))
        self.assertEqual(set(main_entries), {'shared/nous/fixture.json', 'config.yaml'})
        self.assertEqual(set(named_entries), {'own.custom'})
        target = self.root / 'main-restored'
        core.restore(main_result['path'], self.key, target)
        self.assertEqual((target / 'shared/nous/fixture.json').read_bytes(), b'fixture')

    def test_explicit_exclusions_and_nested_scope(self):
        omitted = ['cache/a', 'logs/a', 'tmp/a', 'temp/a', 'backups/a',
                   'profiles/other/config.yaml', 'hermes-agent/source.py',
                   'project/__pycache__/a', 'project/.cache/a', 'project/.pytest_cache/a',
                   'project/node_modules/a', 'project/.venv/a', 'project/venv/a']
        for name in omitted:
            self.put(name)
        self.put('project/.git/HEAD')
        self.put('project/backups/valuable')
        result = self.backup()
        entries = core.validate_archive(core.decrypt(result['path'], self.key))
        self.assertEqual(set(entries), {'project/.git/HEAD', 'project/backups/valuable'})
        for name in omitted:
            with self.subTest(name=name), self.assertRaises(core.FinixError):
                core.validate_archive(self.fixture({name: b'fixture'}))

    def test_unknown_symlink_and_special_fail_before_pruning(self):
        self.put('custom')
        old = self.backup()
        index = (self.state / 'index.json').read_bytes()
        bad = self.source / 'arbitrary-link'
        bad.symlink_to(self.root / 'outside')
        for kind in ('link', 'fifo'):
            if kind == 'fifo':
                bad.unlink()
                os.mkfifo(bad)
            with self.subTest(kind=kind), self.assertRaises(core.FinixError):
                self.backup()
            self.assertTrue(Path(old['path']).exists())
            self.assertEqual((self.state / 'index.json').read_bytes(), index)

    def test_unknown_file_beyond_old_buffer_cap_is_not_omitted(self):
        self.put('custom')
        old = self.backup()
        self.put('ai-projects/oversize', b'0' * 32769)
        with mock.patch.object(core, 'MAX_BYTES', 32768):
            result = self.backup()
            target = self.root / 'unbounded-restored'
            core.restore(result['path'], self.key, target)
        self.assertFalse(Path(old['path']).exists())  # Successful keep=1 backup replaces old copy.
        self.assertEqual((target / 'ai-projects/oversize').stat().st_size, 32769)

    def test_legacy_restore_remains_scoped_and_new_format_required(self):
        data = self.fixture({'config.yaml': b'fixture'}, 'finix-1')
        encrypted = self.root / 'legacy.age'
        encrypted.write_bytes(core.run_age(['-r', self.recipient], data))
        target = self.root / 'legacy-restored'
        core.restore(encrypted, self.key, target)
        self.assertEqual((target / 'config.yaml').read_bytes(), b'fixture')
        with self.assertRaises(core.FinixError):
            core.validate_archive(self.fixture({'unknown/file': b'fixture'}, 'finix-1'))
        with self.assertRaises(core.FinixError):
            core.validate_archive(self.fixture({'plugins/x/.git/HEAD': b'fixture'}, 'finix-1'))

    def test_new_format_rejects_traversal_duplicates_links_devices_and_conflicts(self):
        for name in ('../outside', '/absolute', 'a/../b', 'a//b', 'a/./b', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(core.FinixError):
                core.validate_archive(self.fixture({name: b'fixture'}))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.REGTYPE):
            info = tarfile.TarInfo('profile/custom')
            info.type = kind
            info.linkname = 'profile/other' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
            with self.subTest(kind=kind), self.assertRaises(core.FinixError):
                core.validate_archive(self.fixture({'custom': b'fixture'}, extra=[info]))
        with self.assertRaises(core.FinixError):
            core.validate_archive(self.fixture({'custom': b'fixture', 'custom/child': b'fixture'}))


if __name__ == '__main__':
    unittest.main()
