"""Synthetic shared per-file captures, including exact skill preferences."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from finix import core, streaming

OLD = b'{ "skill": {"pinned":true,"locked":true,"unknown":[1,"keep"]}}\n'
NEW = b'{"skill":{"pinned":false,"locked":true,"count":900}}'

class LiveJSONTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = core.private_dir(self.base / 'profile')
        (self.home / 'skills').mkdir()
        self.path = self.home / 'skills/.usage.json'
        self.path.write_bytes(OLD)
        self.stage = core.private_dir(self.home / 'cache/finix/sqlite-temp')

    def test_latest_atomic_generation_all_archive_paths(self):
        original = core.scan
        for mode in ('buffered', 'streaming', 'multiroot'):
            with self.subTest(mode=mode):
                self.path.write_bytes(OLD)
                def scan(*args, **kwargs):
                    result = original(*args, **kwargs)
                    replacement = self.base / 'replacement'
                    replacement.write_bytes(NEW)
                    os.replace(replacement, self.path)
                    return result
                extra = core.private_dir(self.base / 'extra')
                with core.locked(self.home) as anchor, core.locked(extra) as other, patch.object(core, 'scan', scan):
                    if mode == 'buffered':
                        archive = core.make_archive(anchor)
                    else:
                        output = io.BytesIO()
                        streaming.write_archive(anchor, output, extra_fds=([({'name':'projects','path':str(extra)},other)] if mode == 'multiroot' else []))
                        archive = output.getvalue()
                key = 'profile/skills/.usage.json' if mode == 'multiroot' else 'skills/.usage.json'
                self.assertEqual(core.validate_archive(archive)[key], NEW)

    def test_encrypted_restore_preserves_preferences_exactly(self):
        recipient = core.keygen(self.base / 'identity')
        result = core.backup(self.home, self.base / 'out', self.base / 'state', recipient)
        core.restore(result['path'], self.base / 'identity', self.base / 'restored')
        self.assertEqual((self.base / 'restored/skills/.usage.json').read_bytes(), OLD)

    def test_ordinary_before_after_capture_changes_do_not_invalidate_copy(self):
        path = self.home / 'ordinary'
        path.write_bytes(b'discovered')
        original, add_original = core.scan, streaming._add_payload
        def scan(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(b'captured')
            return result
        def add(*args, **kwargs):
            result = add_original(*args, **kwargs)
            if args[1] == 'ordinary':
                path.write_bytes(b'later')
                (self.home / 'new').write_bytes(b'outside discovery scope')
            return result
        with core.locked(self.home) as anchor, patch.object(core, 'scan', scan), patch.object(streaming, '_add_payload', add):
            output = io.BytesIO()
            streaming.write_archive(anchor, output)
        files = core.validate_archive(output.getvalue())
        self.assertEqual(files['ordinary'], b'captured')
        self.assertNotIn('new', files)

    def test_concurrent_atomic_writer_after_capture_does_not_invalidate_backup(self):
        import threading
        start, done = threading.Event(), threading.Event()
        failures = []
        def writer():
            try:
                if not start.wait(5):
                    raise AssertionError('capture never completed')
                replacement = self.base / 'writer-generation'
                replacement.write_bytes(NEW)
                os.replace(replacement, self.path)
            except BaseException as exc:
                failures.append(exc)
            finally:
                done.set()
        thread = threading.Thread(target=writer)
        original = streaming._add_payload
        def add(*args, **kwargs):
            start.set()
            self.assertTrue(done.wait(5))
            return original(*args, **kwargs)
        thread.start()
        try:
            with core.locked(self.home) as anchor, patch.object(streaming, '_add_payload', add):
                output = io.BytesIO()
                streaming.write_archive(anchor, output)
            self.assertEqual(core.validate_archive(output.getvalue())['skills/.usage.json'], OLD)
            self.assertEqual(self.path.read_bytes(), NEW)
            self.assertEqual(failures, [])
        finally:
            start.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_no_json_parse_reserialize(self):
        for data in (b'{"partial":', b'\xff arbitrary', NEW):
            self.path.write_bytes(data)
            with core.locked(self.home) as anchor:
                archive = core.make_archive(anchor)
            self.assertEqual(core.validate_archive(archive)['skills/.usage.json'], data)

    def test_missing_before_capture_is_explicitly_incomplete(self):
        original = core.scan
        def scan(*args, **kwargs):
            result = original(*args, **kwargs)
            self.path.unlink()
            return result
        with core.locked(self.home) as anchor, patch.object(core, 'scan', scan):
            outcome = streaming.write_archive(anchor, io.BytesIO())
            self.assertEqual(outcome, {'complete': False, 'omitted_count': 1})

    def test_unsafe_replacement_before_capture_rejected(self):
        original = core.scan
        def scan(*args, **kwargs):
            result = original(*args, **kwargs)
            self.path.unlink()
            self.path.mkdir()
            return result
        with core.locked(self.home) as anchor, patch.object(core, 'scan', scan):
            with self.assertRaises((core.FinixError, OSError)):
                core.make_archive(anchor)

    def test_link_or_reparse_replacement_after_discovery_rejected(self):
        original = core.scan
        target = core.private_dir(self.base / 'outside')
        (target / 'secret').write_bytes(b'not a source')
        def scan(*args, **kwargs):
            result = original(*args, **kwargs)
            self.path.unlink()
            if os.name == 'nt':
                import subprocess
                subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(self.path), str(target)], check=True, capture_output=True)
            else:
                self.path.symlink_to(target)
            return result
        with core.locked(self.home) as anchor, patch.object(core, 'scan', scan):
            with self.assertRaises((core.FinixError, OSError)):
                streaming.write_archive(anchor, io.BytesIO())

    def test_completed_capture_does_not_recheck_removed_source(self):
        original = streaming._add_payload
        def add(*args, **kwargs):
            result = original(*args, **kwargs)
            self.path.unlink()
            return result
        with core.locked(self.home) as anchor, patch.object(streaming, '_add_payload', add):
            output = io.BytesIO()
            streaming.write_archive(anchor, output)
        self.assertEqual(core.validate_archive(output.getvalue())['skills/.usage.json'], OLD)

    def _race(self, continuous=False, sqlite=False):
        read, calls = os.read, []
        with core.locked(self.home) as anchor:
            expected = core.scan(anchor)['skills/.usage.json']
            with core.source_file(anchor, 'skills/.usage.json', expected) as (fd, parent, leaf):
                def mutate(target, size):
                    position = os.lseek(target, 0, os.SEEK_CUR) if target == fd else -1
                    data = read(target, size)
                    if target == fd and position == 0 and data and (continuous or not calls):
                        self.path.write_bytes((b'SQLite format 3\x00' + bytes(8192)) if sqlite else NEW + b' ' * len(calls))
                        calls.append(1)
                    return data
                with patch.object(os, 'read', mutate):
                    with streaming.ordinary_payload(fd, parent, leaf) as (stream, size):
                        self.assertEqual(stream.read(), NEW)
                        self.assertEqual(size, len(NEW))
        return calls

    def test_continuous_writer_bounded_error(self):
        with self.assertRaisesRegex(core.SourceCaptureError, '^SOURCE_UNSTABLE_CAPTURE$'):
            self._race(continuous=True)

    def test_retry_captures_stabilized_bytes_not_failed_attempt(self):
        self.assertEqual(len(self._race()), 1)

    def test_sqlite_classification_change_during_copy_rejected(self):
        with self.assertRaisesRegex(core.FinixError, '^SOURCE_CLASSIFICATION_CHANGED_DURING_CAPTURE$'):
            self._race(sqlite=True)

    def test_large_ordinary_only_configured_private_staging(self):
        data = b'large ordinary' * 100
        self.path.write_bytes(data)
        with core.locked(self.home) as anchor:
            expected = core.scan(anchor)['skills/.usage.json']
            with core.source_file(anchor, 'skills/.usage.json', expected) as (fd, parent, leaf):
                with self.assertRaisesRegex(core.FinixError, '^NEED_SQLITE_TEMP_DIRECTORY$'):
                    with streaming.ordinary_payload(fd, parent, leaf, memory_limit=16):
                        pass
                with streaming.ordinary_payload(fd, parent, leaf, self.stage, memory_limit=16) as (stream, size):
                    self.assertEqual(stream.read(), data)
                    self.assertEqual(size, len(data))
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_capture_failure_preserves_previous_complete_backup_and_index(self):
        recipient = core.keygen(self.base / 'identity')
        result = core.backup(self.home, self.base / 'out', self.base / 'state', recipient)
        previous = Path(result['path'])
        digest = core.sha256_file(previous)
        index = (self.base / 'state/index.json').read_bytes()
        with patch.object(streaming, 'ordinary_payload', side_effect=core.FinixError('SOURCE_UNSTABLE_DURING_CAPTURE')):
            with self.assertRaisesRegex(core.FinixError, '^SOURCE_UNSTABLE_DURING_CAPTURE$'):
                core.backup(self.home, self.base / 'out', self.base / 'state', recipient, keep=1)
        self.assertEqual(core.sha256_file(previous), digest)
        self.assertEqual((self.base / 'state/index.json').read_bytes(), index)
        self.assertEqual(list((self.base / 'out').glob('*.age')), [previous])

    @unittest.skipIf(os.name == 'nt', 'Windows pinned handles deny delete sharing')
    def test_path_identity_substitution_during_copy_rejected(self):
        read = os.read
        with core.locked(self.home) as anchor:
            expected = core.scan(anchor)['skills/.usage.json']
            with core.source_file(anchor, 'skills/.usage.json', expected) as (fd, parent, leaf):
                changed = []
                def mutate(target, size):
                    data = read(target, size)
                    if target == fd and data and not changed:
                        replacement = self.base / 'replacement'
                        replacement.write_bytes(NEW)
                        os.replace(replacement, self.path)
                        changed.append(1)
                    return data
                with patch.object(os, 'read', mutate):
                    with self.assertRaisesRegex(core.FinixError, '^SOURCE_REPLACED_DURING_CAPTURE$'):
                        with streaming.ordinary_payload(fd, parent, leaf):
                            pass

    def test_closed_capture_error_codes_do_not_contain_paths(self):
        from finix.setup_errors import setup_error_code
        self.assertEqual(setup_error_code(core.FinixError('SOURCE_UNSTABLE_DURING_CAPTURE')), 'SETUP_SOURCE_CHANGED')
        self.assertEqual(setup_error_code(core.FinixError('SOURCE_UNSTABLE_DURING_CAPTURE: private')), 'SETUP_CHECK_FAILED')

    def test_fake_sqlite_new_generation_fails_validation(self):
        original = core.scan
        def scan(*args, **kwargs):
            result = original(*args, **kwargs)
            self.path.write_bytes(b'SQLite format 3\x00' + bytes(8192))
            return result
        with core.locked(self.home) as anchor, patch.object(core, 'scan', scan):
            with self.assertRaises(core.FinixError):
                core.make_archive(anchor)

if __name__ == '__main__':
    unittest.main()
