"""Real age/SQLite + bounded-memory streams; every profile/key is synthetic."""
from contextlib import closing
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tarfile
import tempfile
import threading
import tracemalloc
import unittest
from unittest import mock

from finix import core, streaming

MiB = 1024 * 1024
LARGE = os.environ.get('FINIX_LARGE_TESTS') == '1'


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.key = self.root / 'identity'
        self.recipient = core.keygen(self.key)
        self.output, self.state = self.root / 'output', self.root / 'state'
        self.stage = self.root / 'sqlite-temporary-fixture'
        self.stage.mkdir(mode=0o700)

    def backup(self, **kwargs):
        return core.backup(self.source, self.output, self.state, self.recipient, **kwargs)

    def database(self, size):
        path = self.source / 'store?fixture#%.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table fixture(value blob)')
            db.execute('insert into fixture values(zeroblob(?))', (size,))
            db.commit()
        return path

    def fixture(self, raw):
        path = self.root / 'fixture.age'
        path.write_bytes(core.run_age(['-r', self.recipient], raw))
        return path

    def test_invalid_deflate_is_neutral_and_never_published(self):
        import subprocess
        import sys
        raw = bytes.fromhex('1f8b0800000000000003') + b'\x07' + bytes(8)
        archive = self.fixture(raw)
        before = {p.name for p in self.root.iterdir()}
        target = self.root / 'invalid-restored'
        with self.assertRaises(core.FinixError):
            core.restore(archive, self.key, target)
        self.assertFalse(target.exists())
        self.assertEqual({p.name for p in self.root.iterdir()}, before)
        result = subprocess.run([sys.executable, '-m', 'finix', 'restore',
                                 '--archive', str(archive), '--identity', str(self.key),
                                 '--target', str(target)], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stdout + result.stderr)
        self.assertFalse(target.exists())
        self.assertEqual({p.name for p in self.root.iterdir()}, before)

    def test_empty_and_long_names_restore(self):
        result = self.backup()
        core.restore(result['path'], self.key, self.root / 'empty')
        self.assertEqual(list((self.root / 'empty').iterdir()), [])
        longpath = self.source / ('unicode-имя-' * 10) / ('long-' * 30)
        longpath.parent.mkdir()
        longpath.write_bytes(b'long-name-fixture')
        result = self.backup()
        core.restore(result['path'], self.key, self.root / 'restored')
        self.assertEqual((self.root / 'restored' / longpath.relative_to(self.source)).read_bytes(),
                         b'long-name-fixture')

    def test_production_never_calls_buffered_archive_helpers(self):
        (self.source / 'fixture').write_bytes(b'synthetic')
        with (mock.patch.object(core, 'make_archive', side_effect=AssertionError('buffered')),
              mock.patch.object(core, 'decrypt', side_effect=AssertionError('buffered')),
              mock.patch.object(core, 'validate_archive', side_effect=AssertionError('buffered'))):
            result = self.backup()
            core.restore(result['path'], self.key, self.root / 'restored')
        self.assertEqual((self.root / 'restored/fixture').read_bytes(), b'synthetic')

    def test_unsafe_sqlite_temp_preserves_prior(self):
        (self.source / 'fixture').write_bytes(b'synthetic')
        old = self.backup()
        old_index = (self.state / 'index.json').read_bytes()
        self.database(256 * 1024)
        self.stage.chmod(0o777)
        with self.assertRaisesRegex(core.FinixError, 'NEED_SQLITE_TEMP_DIRECTORY'):
            self.backup(sqlite_temp_dir=self.stage, keep=1)
        self.assertTrue(Path(old['path']).exists())
        self.assertEqual((self.state / 'index.json').read_bytes(), old_index)
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertEqual(list(self.output.iterdir()), [Path(old['path'])])

    def test_temp_readiness_and_no_encrypted_storage_requirement(self):
        self.assertTrue(core.sqlite_temp_readiness(None)['ready'])
        self.assertTrue(core.sqlite_temp_readiness(self.stage)['ready'])
        self.assertIn('plaintext', core.sqlite_temp_readiness(self.stage)['message'])
        link = self.root / 'link'
        link.symlink_to(self.stage)
        self.assertFalse(core.sqlite_temp_readiness(link)['ready'])
        self.stage.chmod(0o777)
        self.assertFalse(core.sqlite_temp_readiness(self.stage)['ready'])
        self.assertFalse(core.sqlite_temp_readiness(self.root / 'missing')['ready'])

    def test_insufficient_space_fails_without_snapshot_or_retention(self):
        (self.source / 'fixture').write_bytes(b'synthetic')
        old = self.backup()
        index = (self.state / 'index.json').read_bytes()
        self.database(256 * 1024)
        usage = shutil.disk_usage(self.stage)
        with mock.patch.object(streaming.shutil, 'disk_usage', return_value=usage._replace(free=1)):
            with self.assertRaisesRegex(core.FinixError, 'NOT_ENOUGH_SQLITE_TEMP_SPACE'):
                self.backup(sqlite_temp_dir=self.stage, keep=1)
        self.assertEqual((self.state / 'index.json').read_bytes(), index)
        self.assertTrue(Path(old['path']).exists())
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_sqlite_stage_is_private_cleanup_on_success_error_cancel(self):
        path = self.database(256 * 1024)
        with mock.patch.object(core, 'sqlite_snapshot', side_effect=AssertionError('RAM serialize')):
            inspected = []
            def progress(record):
                if record['phase'] == 'sqlite':
                    for directory in self.stage.iterdir():
                        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                        for file in directory.iterdir():
                            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                        inspected.append(directory)
            result = self.backup(sqlite_temp_dir=self.stage, progress=progress)
            self.assertTrue(inspected)
            self.assertEqual(list(self.stage.iterdir()), [])
            core.restore(result['path'], self.key, self.root / 'restored')
            with closing(sqlite3.connect(self.root / 'restored' / path.name)) as db:
                self.assertEqual(db.execute('pragma integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('select length(value) from fixture').fetchone()[0], 256 * 1024)
            old_index = (self.state / 'index.json').read_bytes()
            def fail_progress(record):
                if record['phase'] == 'sqlite':
                    raise core.FinixError('synthetic disk failure')
            with self.assertRaises(core.FinixError):
                self.backup(sqlite_temp_dir=self.stage, progress=fail_progress, keep=1)
            self.assertEqual(list(self.stage.iterdir()), [])
            event = threading.Event()
            def cancel_progress(record):
                if record['phase'] == 'sqlite':
                    event.set()
            with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                self.backup(sqlite_temp_dir=self.stage, progress=cancel_progress, cancel=event, keep=1)
            self.assertEqual(list(self.stage.iterdir()), [])
            self.assertEqual((self.state / 'index.json').read_bytes(), old_index)

    def test_pinned_sqlite_wal_view_survives_concurrent_committed_writer(self):
        path = self.source / 'wal-store'
        with closing(sqlite3.connect(path)) as writer:
            writer.execute('pragma journal_mode=WAL')
            writer.execute('pragma wal_autocheckpoint=0')
            writer.execute('create table fixture(value blob)')
            writer.execute('insert into fixture values(zeroblob(?))', (2 * MiB,))
            writer.commit()
            changed = []
            def progress(record):
                if record['phase'] == 'sqlite' and not changed:
                    writer.execute("insert into fixture values ('committed-after-read-view')")
                    writer.commit()
                    changed.append(True)
            result = self.backup(sqlite_temp_dir=self.stage, progress=progress)
            self.assertTrue(changed)
            self.assertEqual(writer.execute('select count(*) from fixture').fetchone()[0], 2)
        core.restore(result['path'], self.key, self.root / 'restored')
        with closing(sqlite3.connect(self.root / 'restored/wal-store')) as restored:
            self.assertEqual(restored.execute('pragma integrity_check').fetchone()[0], 'ok')
            self.assertEqual(restored.execute('select count(*) from fixture').fetchone()[0], 1)
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_cancellation_during_streaming_keeps_restore_target_and_prior(self):
        with (self.source / 'fixture').open('wb') as stream:
            stream.truncate(5 * MiB)
        old = self.backup()
        old_index = (self.state / 'index.json').read_bytes()
        event = threading.Event()
        def progress(record):
            if record['bytes'] > MiB:
                event.set()
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            self.backup(cancel=event, progress=progress, keep=1)
        self.assertEqual((self.state / 'index.json').read_bytes(), old_index)
        event.clear()
        target = self.root / 'restore'
        target.mkdir()
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            core.restore(old['path'], self.key, target, cancel=event, progress=progress)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list(self.root.glob('.finix-restore-*')), [])

    def test_checksum_cancellation_does_not_commit_or_prune(self):
        (self.source / 'fixture').write_bytes(os.urandom(2 * MiB))
        old = self.backup()
        old_index = (self.state / 'index.json').read_bytes()
        event = threading.Event()
        def progress(record):
            if record['phase'] == 'checksum':
                event.set()
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            self.backup(cancel=event, progress=progress, keep=1)
        self.assertEqual((self.state / 'index.json').read_bytes(), old_index)
        self.assertEqual(list(self.output.iterdir()), [Path(old['path'])])

    def test_crc_truncation_trailing_data_and_hostile_headers_do_not_publish(self):
        (self.source / 'fixture').write_bytes(b'synthetic')
        result = self.backup()
        good = core.decrypt(result['path'], self.key)
        crc = bytearray(good)
        crc[-8] ^= 1
        inputs = [good[:-4], bytes(crc), good + b'not-gzip',
                  gzip.compress(gzip.decompress(good) + b'trailing-nonzero')]
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE,
                     tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME):
            header = tarfile.TarInfo('profile/hostile')
            header.type = kind
            header.size = 1 << 40
            inputs.append(gzip.compress(header.tobuf(format=tarfile.GNU_FORMAT)))
        for index, data in enumerate(inputs):
            with self.subTest(index=index):
                path = self.fixture(data)
                target = self.root / f'restore-{index}'
                with self.assertRaises(core.FinixError):
                    core.restore(path, self.key, target)
                self.assertFalse(target.exists())
                self.assertEqual(list(self.root.glob('.finix-restore-*')), [])

    @unittest.skipUnless(LARGE, 'opt-in real >128 MiB encrypted archive')
    def test_incompressible_archive_over_128mib_bounded_memory_roundtrip(self):
        path = self.source / 'random-fixture'
        with path.open('wb') as stream:
            for _ in range(129):
                stream.write(os.urandom(MiB))
        tracemalloc.start()
        try:
            result = self.backup(sqlite_temp_dir=self.stage)
            self.assertGreater(Path(result['path']).stat().st_size, 128 * MiB)
            core.restore(result['path'], self.key, self.root / 'restored')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 24 * MiB)
        self.assertEqual(core.sha256_file(path), core.sha256_file(self.root / 'restored' / path.name))
        print(f'\nSTREAM_EVIDENCE archive_bytes={Path(result["path"]).stat().st_size} python_peak={peak}')

    @unittest.skipUnless(LARGE, 'opt-in real SQLite >128 MiB')
    def test_sqlite_over_128mib_consistent_disk_snapshot_bounded_memory(self):
        path = self.database(132 * MiB)
        tracemalloc.start()
        try:
            with mock.patch.object(core, 'sqlite_snapshot', side_effect=AssertionError('RAM serialize')):
                result = self.backup(sqlite_temp_dir=self.stage)
            core.restore(result['path'], self.key, self.root / 'restored')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 24 * MiB)
        restored = self.root / 'restored' / path.name
        self.assertGreater(restored.stat().st_size, 128 * MiB)
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('select length(value) from fixture').fetchone()[0], 132 * MiB)
        self.assertEqual(list(self.stage.iterdir()), [])
        print(f'\nSTREAM_EVIDENCE sqlite_bytes={restored.stat().st_size} python_peak={peak} private_plaintext_stage=owner_approved')

    @unittest.skipUnless(LARGE, 'opt-in 8 GiB logical sparse-file roundtrip')
    def test_eight_gib_sparse_file_roundtrip_without_memory_or_ustar_ceiling(self):
        size = 8 * 1024 * MiB + 17
        path = self.source / 'eight-gib-sparse'
        with path.open('wb') as stream:
            stream.truncate(size)
            stream.seek(size - 8)
            stream.write(b'END-SIZE')
        tracemalloc.start()
        try:
            result = self.backup()
            core.restore(result['path'], self.key, self.root / 'restored')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 24 * MiB)
        restored = self.root / 'restored' / path.name
        self.assertEqual(restored.stat().st_size, size)
        self.assertLess(restored.stat().st_blocks * 512, 4 * MiB)
        self.assertEqual(core.sha256_file(path), core.sha256_file(restored))
        print(f'\nSTREAM_EVIDENCE sparse_logical_bytes={size} restored_disk_bytes={restored.stat().st_blocks * 512} python_peak={peak}')


if __name__ == '__main__':
    unittest.main()
