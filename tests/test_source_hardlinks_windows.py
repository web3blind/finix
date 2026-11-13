"""Native NTFS hardlinks are inert source bytes, never generic input authority."""
import io
import os
from pathlib import Path
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'native NTFS contract')
class SourceHardlinkTests(unittest.TestCase):
    def test_actual_sdk_shaped_hardlinks_archive_as_independent_regular_bytes(self):
        from finix import core, streaming, windows_native as win
        import tarfile
        with tempfile.TemporaryDirectory() as temp:
            base = win.private_dir(Path(temp))
            source = win.private_dir(base / 'source')
            site = source / ('installs/' + '1' * 16 + '/pm-runtime/generations/'
                             + '2' * 32 + '/Lib/site-packages/packaging')
            win.private_dir(site)
            data = b'# synthetic package data, never imported\n'
            cache = base / 'uv-cache-entry'
            cache.write_bytes(data)
            leaf = site / '__init__.py'
            os.link(cache, leaf)
            os.link(cache, site / 'second.py')
            self.assertEqual(leaf.stat().st_nlink, 3)
            archive = io.BytesIO()
            with win.directory(source) as anchor:
                inventory = core.scan(anchor)
                self.assertEqual(len(inventory), 2)
                streaming.write_archive(anchor, archive, win.private_dir(base / 'staging'))
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode='r:gz') as tar:
                payloads = [m for m in tar if m.name.startswith('profile/')]
                self.assertEqual(len(payloads), 2)
                self.assertTrue(all(m.isreg() and not m.islnk() for m in payloads))
            archive.seek(0)
            stage = win.private_dir(base / 'restore-stage')
            payload, count, _, completion = streaming.restore_stream(archive, stage)
            self.assertEqual(count, 2)
            self.assertTrue(completion['complete'])
            restored = payload / leaf.relative_to(source)
            other = restored.with_name('second.py')
            self.assertEqual(restored.read_bytes(), data)
            self.assertEqual(other.read_bytes(), data)
            self.assertEqual(restored.stat().st_nlink, 1)
            self.assertEqual(other.stat().st_nlink, 1)
            self.assertFalse(restored.samefile(other))
            self.assertFalse(restored.samefile(cache))
            self.assertEqual(cache.read_bytes(), data)

    def test_generic_crypto_and_executable_openers_still_reject_actual_hardlinks(self):
        from finix import core, windows_native as win
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            base = win.private_dir(Path(temp))
            leaf, alias = base / 'age.exe', base / 'alias'
            leaf.write_bytes(b'synthetic, never executed')
            os.link(leaf, alias)
            for opener in (win.regular, win.snapshot_regular, core.regular_input):
                with self.subTest(opener=opener.__name__), self.assertRaisesRegex(OSError, 'hardlinks'):
                    with opener(leaf):
                        self.fail('generic hardlink admitted')
            with self.assertRaisesRegex(OSError, 'hardlinks'):
                win.trusted_binary(leaf)
            with self.assertRaisesRegex(OSError, 'hardlinks'):
                core.sha256_file(leaf)
            single = base / 'single'
            single.write_bytes(b'synthetic key/archive, must not be consumed')
            with patch.object(win, 'popen', side_effect=AssertionError('crypto dispatched')):
                for archive, key in ((leaf, single), (single, leaf)):
                    with self.subTest(archive=archive.name), self.assertRaisesRegex(OSError, 'hardlinks'):
                        with win.decrypted_stream(archive, key):
                            self.fail('linked crypto input admitted')
            with win.source_snapshot_regular(leaf) as fd:
                self.assertEqual(os.read(fd, 64), b'synthetic, never executed')

    def test_hardlinked_sqlite_is_rejected_before_any_path_based_connection(self):
        from finix import core, windows_native as win
        from unittest.mock import patch
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            base = win.private_dir(Path(temp))
            source = win.private_dir(base / 'source')
            db = source / 'state.db'
            connection = sqlite3.connect(db)
            connection.execute('create table synthetic(value)')
            connection.commit()
            connection.close()
            with win.directory(source) as anchor:
                self.assertTrue(core.scan(anchor)['state.db'].sqlite)
            os.link(db, base / 'outside-alias')
            with patch.object(win, 'connect_source', side_effect=AssertionError('SQLite path reopened')):
                with win.directory(source) as anchor, self.assertRaisesRegex(OSError, 'Hardlinked SQLite'):
                    core.scan(anchor)

    def test_actual_alias_writer_is_excluded_and_capture_releases_source(self):
        from finix import core, streaming, windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            base = win.private_dir(Path(temp))
            source = win.private_dir(base / 'source')
            leaf, alias = source / 'ordinary', base / 'outside-alias'
            leaf.write_bytes(b'captured generation')
            os.link(leaf, alias)
            with alias.open('r+b'):
                with self.assertRaises(OSError):
                    with win.source_snapshot_regular(leaf):
                        self.fail('existing alias writer admitted')
            with win.source_snapshot_regular(leaf) as fd:
                self.assertEqual(os.read(fd, 64), b'captured generation')
                with self.assertRaises(OSError):
                    alias.write_bytes(b'forbidden writer')
                with self.assertRaises(OSError):
                    source.rename(base / 'renamed')
                with self.assertRaises(OSError):
                    leaf.unlink()
            with win.directory(source) as anchor:
                inventory = core.scan(anchor)
                with streaming.file_payload(anchor, 'ordinary', inventory['ordinary']) as (stream, size, _):
                    alias.write_bytes(b'later generation')
                    self.assertEqual(stream.read(), b'captured generation')
                    self.assertEqual(size, len(b'captured generation'))
            self.assertEqual(leaf.read_bytes(), b'later generation')

    def test_shared_leaf_acl_and_writable_ancestor_fail_closed(self):
        from finix import windows_native as win
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            base = win.private_dir(Path(temp))
            parent = win.private_dir(base / 'parent')
            source = win.private_dir(parent / 'source')
            leaf, alias = source / 'ordinary', base / 'alias'
            leaf.write_bytes(b'synthetic')
            os.link(leaf, alias)
            with win.source_snapshot_regular(leaf):
                pass
            for target, grant in ((alias, '*S-1-1-0:(W)'), (parent, '*S-1-1-0:(DC)')):
                with self.subTest(target=target.name):
                    result = subprocess.run(['icacls', str(target), '/grant', grant], capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    try:
                        with self.assertRaisesRegex(OSError, 'untrusted owner or ACL'):
                            with win.source_snapshot_regular(leaf):
                                self.fail('untrusted source admitted')
                    finally:
                        result = subprocess.run(['icacls', str(target), '/remove:g', '*S-1-1-0'], capture_output=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
            with win.source_snapshot_regular(leaf):
                pass

    def test_source_opener_preserves_reparse_ancestor_and_leaf_rejection(self):
        from finix import windows_native as win
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            base = win.private_dir(Path(temp))
            target = win.private_dir(base / 'target')
            leaf = target / 'ordinary'
            leaf.write_bytes(b'synthetic')
            os.link(leaf, base / 'alias')
            junction = base / 'junction'
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaises(OSError):
                    with win.source_snapshot_regular(junction / leaf.name):
                        self.fail('junction ancestor followed')
                with self.assertRaises(OSError):
                    with win.source_snapshot_regular(junction):
                        self.fail('reparse leaf admitted')
            finally:
                junction.rmdir()

    def test_hardlink_copy_retains_private_spill_bounds_and_cancellation(self):
        from finix import core, streaming, windows_native as win
        from unittest.mock import patch
        import threading
        with tempfile.TemporaryDirectory() as temp:
            base = win.private_dir(Path(temp))
            leaf = base / 'ordinary'
            data = b'synthetic data' * 100
            leaf.write_bytes(data)
            os.link(leaf, base / 'alias')
            stage = win.private_dir(base / 'staging')
            reads = []
            real_read = os.read
            def bounded(fd, count):
                reads.append(count)
                return real_read(fd, count)
            with win.source_snapshot_regular(leaf) as fd, win.directory(base) as parent:
                with patch.object(os, 'read', bounded):
                    with streaming.ordinary_payload(fd, parent, leaf.name, stage, memory_limit=1) as (stream, size):
                        self.assertEqual(stream.read(), data)
                        self.assertEqual(size, len(data))
                self.assertTrue(reads)
                self.assertLessEqual(max(reads), min(streaming.CHUNK, len(data) + 1))
                self.assertEqual(list(stage.iterdir()), [])
                cancel = threading.Event()
                cancel.set()
                with self.assertRaisesRegex(core.FinixError, 'Operation cancelled'):
                    with streaming.ordinary_payload(fd, parent, leaf.name, stage, cancel, memory_limit=1):
                        self.fail('cancelled copy admitted')
                self.assertEqual(list(stage.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
