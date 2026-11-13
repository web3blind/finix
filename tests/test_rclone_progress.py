"""No network: real pipes from a Python fake child, never a provider executable."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from finix import core
from finix.rclone_storage import RcloneRunner, RcloneError


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = core.private_dir(Path(self.temp.name) / 'private')
        auth = core.private_dir(self.root / 'auth')
        self.config = auth / 'fixture.conf'
        with core.regular_output(self.config) as fd:
            os.write(fd, b'[cloud]\ntype=yandex\n')
        self.binary = self.root / 'offline-fixture.exe'
        with core.regular_output(self.binary) as fd:
            os.write(fd, b'NEVER EXECUTE: fixture sentinel')
        self.binary.chmod(0o700)
        self.script = self.root / 'child.py'
        self.events = []
        self.argv = []

    def run_child(self, script, operation='copyto', **kwargs):
        self.script.write_text(script)
        runner = RcloneRunner(self.root, self.config, binary=str(self.binary), transfer_timeout=kwargs.pop('transfer_timeout', 5))
        if os.name == 'nt':
            from finix import windows_native as win
            original = win.popen
            target = 'finix.windows_native.popen'
        else:
            original = subprocess.Popen
            target = 'finix.rclone_storage.subprocess.Popen'
        def launch(argv, **opts):
            self.assertEqual(argv[0], str(self.binary))
            self.argv = argv
            self.assertFalse(any(k.startswith(('RCLONE_', 'AWS_')) for k in opts['env']))
            return original([sys.executable, str(self.script)], **opts)
        with patch(target, side_effect=launch):
            if os.name == 'nt':
                with patch('finix.windows_native.trusted_binary', return_value=True):
                    return runner(operation, expected_size=100, progress=self.events.append, **kwargs)
            return runner(operation, expected_size=100, progress=self.events.append, **kwargs)

    def test_split_json_stats_report_real_upload_bytes(self):
        self.run_child('''import os,time
for n in (25, 50, 100):
    row = ('{"stats":{"bytes":%d,"totalBytes":100},"msg":"token private/path"}\\n' % n).encode()
    os.write(2,row[:13]); time.sleep(.02); os.write(2,row[13:])
''')
        self.assertEqual([e['completed_bytes'] for e in self.events], [0, 25, 50, 100, 100])
        self.assertEqual(self.events[-2]['percent'], 99)
        self.assertEqual(self.events[-1]['percent'], 100)
        self.assertTrue(all(set(e) == {'phase', 'completed_bytes', 'total_bytes', 'percent'} for e in self.events))
        self.assertTrue(all(e['phase'] == 'upload' for e in self.events))
        self.assertNotIn('token', json.dumps(self.events))
        self.assertIn('--stats=1s', self.argv)
        self.assertIn('--stats-log-level=ERROR', self.argv)
        self.assertIn('--use-json-log', self.argv)

    def test_transfer_without_deadline_waits_for_real_child(self):
        import itertools
        with patch('finix.rclone_storage.time.monotonic', side_effect=itertools.count(0, 86400)):
            self.run_child('import time; time.sleep(.15)', transfer_timeout=None)
        self.assertEqual(self.events[-1]['percent'], 100)

    def test_cancel_silent_child_is_prompt(self):
        start = time.monotonic()
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            self.run_child('import time; time.sleep(30)', cancel=lambda: time.monotonic() - start > .3)
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(any(e['percent'] == 100 for e in self.events))

    def test_malformed_stats_are_ignored_and_counters_monotonic(self):
        rows = [{'bytes': True, 'totalBytes': 100}, {'bytes': -1, 'totalBytes': 100},
                {'bytes': '20', 'totalBytes': 100}, {'bytes': 90, 'totalBytes': 99},
                {'bytes': 40, 'totalBytes': 100}, {'bytes': 20, 'totalBytes': 100},
                {'bytes': 101, 'totalBytes': 100}, {'bytes': 60, 'totalBytes': 100}]
        script = 'import os,json\nos.write(2,b"malformed private-secret\\n")\n'
        script += 'for row in ' + repr(rows) + ':\n    os.write(2,(json.dumps({"stats":row})+"\\n").encode())\n'
        self.run_child(script)
        self.assertEqual([e['completed_bytes'] for e in self.events], [0, 40, 60, 100])
        self.assertNotIn('private-secret', json.dumps(self.events))

    def test_hours_of_stats_do_not_consume_diagnostic_budget(self):
        self.run_child('''import os
for i in range(10000):
    os.write(2,b'{"stats":{"bytes":50,"totalBytes":100},"msg":"private-token"}\\n')
''')
        self.assertEqual([e['completed_bytes'] for e in self.events], [0, 50, 100])

    def test_nonstats_diagnostics_remain_bounded(self):
        with self.assertRaisesRegex(RcloneError, 'OUTPUT_LIMIT') as caught:
            self.run_child('import os\nfor i in range(2000): os.write(2,b\'{"msg":"private-secret abcdefghijklmnopqrstuvwxyz"}\\n\')')
        self.assertNotIn('private-secret', str(caught.exception))

    def test_single_huge_stats_line_is_bounded(self):
        with self.assertRaisesRegex(RcloneError, 'OUTPUT_LIMIT'):
            self.run_child('import os\nos.write(2,b\'{"stats":{},"msg":"\'+b"x"*70000+b\'"}\\n\')')

    def test_cat_counts_stdout_not_json_and_waits_for_hash_completion(self):
        sink = io.BytesIO()
        self.run_child('''import os,time
os.write(2,b'{"stats":{"bytes":9999,"totalBytes":9999}}\\n')
for i in range(5):
    os.write(1,b'x'*20); time.sleep(.03)
''', operation='cat', output=sink)
        self.assertEqual(len(sink.getvalue()), 100)
        counts = [e['completed_bytes'] for e in self.events]
        self.assertEqual(counts[0], 0)
        self.assertEqual(counts[-1], 100)
        self.assertTrue(any(0 < n < 100 for n in counts))
        self.assertEqual(counts, sorted(counts))
        self.assertTrue(all(e['phase'] == 'verify' and e['percent'] < 100 for e in self.events))

    def test_short_or_failed_child_never_finishes_phase(self):
        for script, operation, code in [
                ('import os; os.write(1,b"short")', 'cat', 'SHORT_DOWNLOAD'),
                ('import os; os.write(2,b\'{"stats":{"bytes":100,"totalBytes":100}}\\n\'); raise SystemExit(1)', 'copyto', 'PROVIDER_ERROR'),
                ('import os; os.write(2,b\'{"status":401,"msg":"private-secret"}\\n\'); raise SystemExit(1)', 'copyto', 'AUTH_REQUIRED')]:
            with self.subTest(code=code):
                self.events.clear()
                with self.assertRaisesRegex(RcloneError, code):
                    self.run_child(script, operation=operation, output=io.BytesIO() if operation == 'cat' else None)
                self.assertFalse(any(e['percent'] == 100 for e in self.events))

    def test_cancellation_cleans_spawned_descendants(self):
        marker, ready = self.root / 'escaped.txt', self.root / 'ready.txt'
        grandchild = 'from pathlib import Path; import time; Path(' + repr(str(ready)) + ').write_text("ready"); time.sleep(1); Path(' + repr(str(marker)) + ').write_text("escaped")'
        script = 'import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",' + repr(grandchild) + '])\ntime.sleep(30)'
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            self.run_child(script, cancel=ready.exists)
        self.assertTrue(ready.exists(), 'grandchild must actually start')
        time.sleep(1.2)
        self.assertFalse(marker.exists(), 'child escaped process-group/Job Object cleanup')

    def test_pre_cancel_does_not_launch_child(self):
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            self.run_child('raise AssertionError("must not execute")', cancel=lambda: True)
        self.assertEqual(self.argv, [])
        self.assertEqual(self.events, [])


class AdapterProgressTests(unittest.TestCase):
    from tests.test_rclone_pending import PendingTests as _Fixture
    setUp = _Fixture.setUp
    adapter = _Fixture.adapter
    pending = _Fixture.pending

    def test_cloud_hash_reports_real_bounded_bytes(self):
        from finix import cloud_files
        self.cipher.write_bytes(b'age-encryption.org/v1\n' + b'x' * (cloud_files.CHUNK * 2))
        events = []
        result = cloud_files.describe(self.cipher, native=True, progress=events.append)
        self.assertEqual(events[0]['completed_bytes'], 0)
        self.assertEqual(events[-1]['completed_bytes'], result['size'])
        self.assertEqual(events[-1]['percent'], 100)
        self.assertEqual(len(events), 5)
        self.assertEqual([e['completed_bytes'] for e in events], sorted(e['completed_bytes'] for e in events))

    def test_cloud_copy_cancellation_removes_partial_target(self):
        from finix import cloud_files
        target = self.root / 'partial.age'
        events = []
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            cloud_files.copy(self.cipher, target, progress=events.append,
                             cancel=lambda: any(e['completed_bytes'] > 0 for e in events))
        self.assertFalse(target.exists())
        self.assertFalse(any(e['percent'] == 100 for e in events))

    def test_cloud_verified_hash_mismatch_never_reports_completion(self):
        from finix import cloud_files
        events = []
        expected = dict(size=self.cipher.stat().st_size, sha256='0' * 64)
        with self.assertRaisesRegex(core.FinixError, 'readback mismatch'):
            cloud_files.verified(self.cipher, expected, progress=events.append)
        self.assertFalse(any(e['percent'] == 100 for e in events))

    def test_cloud_copy_reports_completion_after_hash(self):
        from finix import cloud_files
        events = []
        target = self.root / 'copy.age'
        cloud_files.copy(self.cipher, target, progress=events.append)
        self.assertEqual(events[0]['percent'], 0)
        self.assertEqual(events[-1]['percent'], 100)
        self.assertEqual(target.read_bytes(), self.cipher.read_bytes())

    def test_pre_cancel_does_not_touch_transport_or_intent(self):
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            self.drive.backup_cipher(self.cipher, cancel=lambda: True)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.state.exists())

    def test_cancel_during_local_hash_is_checked_between_chunks(self):
        from finix import cloud_files
        calls = []
        def cancel():
            calls.append(True)
            return len(calls) >= 4
        with patch.object(cloud_files, 'CHUNK', 32):
            with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                self.drive.backup_cipher(self.cipher, cancel=cancel)
        self.assertEqual(self.transport.calls, [])

    def test_cancel_after_upload_preserves_intent_and_previous_copy(self):
        first = self.drive.backup_cipher(self.cipher, completion=self.completion)
        original = self.transport
        cancelled = []
        def run(op, *args, progress=None, cancel=None, **kwargs):
            result = original(op, *args, **kwargs)
            if op == 'copyto':
                cancelled.append(True)
            return result
        self.drive.runner = run
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            self.drive.backup_cipher(self.cipher, keep=1, completion=self.completion, cancel=lambda: bool(cancelled))
        intent = self.pending()['record']
        self.assertIn(self.drive.object_path(first), original.objects)
        self.assertEqual([r['id'] for r in self.drive.ledger()['records']], [first['id']])
        self.progress_transport()
        events = []
        result = self.drive.reconcile(progress=events.append)
        self.assertEqual(result['id'], intent['id'])
        self.assertEqual(events[-1]['percent'], 100)
        copies = [op for op, args in original.calls if op == 'copyto' and args[1] == self.drive.object_path(intent)]
        self.assertEqual(len(copies), 1)
        # Cancellation at verified-byte completion must still precede ledger
        # publication and retention. 100 here is phase-local, never job success.
        events.clear()
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            self.drive.backup_cipher(self.cipher, keep=1, completion=self.completion,
                progress=events.append,
                cancel=lambda: bool(events and events[-1]['phase'] == 'verify' and events[-1]['percent'] == 100))
        self.assertTrue((self.state / 'rclone-pending.json').exists())
        self.assertEqual(len(self.drive.ledger()['records']), 2)
        self.assertFalse(any(op == 'deletefile' for op, _ in original.calls))

    def test_bad_readback_never_finishes_verify_or_prunes(self):
        self.progress_transport()
        original = self.drive.runner
        def corrupt(op, *args, **kwargs):
            if op == 'cat':
                key = args[0]
                self.transport.objects[key] = self.transport.objects[key][:-1] + b'X'
            return original(op, *args, **kwargs)
        self.drive.runner = corrupt
        events = []
        with self.assertRaisesRegex(core.FinixError, 'readback mismatch'):
            self.drive.backup_cipher(self.cipher, progress=events.append)
        self.assertTrue((self.state / 'rclone-pending.json').exists())
        self.assertFalse(any(e['phase'] == 'verify' and e['percent'] == 100 for e in events))

    def test_runner_typeerror_is_not_retried_or_masked(self):
        calls = []
        def broken(*args, **kwargs):
            calls.append(True)
            raise TypeError('internal fixture bug')
        self.drive.runner = broken
        with self.assertRaisesRegex(TypeError, 'internal fixture bug'):
            self.drive.backup_cipher(self.cipher, progress=lambda e: None)
        self.assertEqual(len(calls), 1)

    def test_legacy_memory_runner_needs_no_new_keywords(self):
        result = self.drive.backup_cipher(self.cipher)
        self.assertTrue(self.drive.download(result)['verified'])

    def progress_transport(self):
        original = self.transport
        def run(op, *args, output=None, expected_size=None, progress=None, cancel=None):
            if op == 'copyto':
                self.assertEqual(expected_size, self.cipher.stat().st_size)
                if progress:
                    for n in (0, expected_size // 2, expected_size):
                        progress(dict(phase='upload', completed_bytes=n, total_bytes=expected_size,
                                      percent=min(99, n * 100 // expected_size)))
            if op == 'cat' and progress:
                for n in (0, expected_size // 2, expected_size):
                    progress(dict(phase='verify', completed_bytes=n, total_bytes=expected_size,
                                  percent=min(99, n * 100 // expected_size)))
            return original(op, *args, output=output, expected_size=expected_size)
        self.drive.runner = run

    def test_adapter_phases_use_frozen_total_and_hash_gate(self):
        self.progress_transport()
        events = []
        row = self.drive.backup_cipher(self.cipher, completion=self.completion, progress=events.append)
        self.assertTrue(row['verified'])
        self.assertEqual(events[-1], dict(phase='verify', completed_bytes=row['size'], total_bytes=row['size'], percent=100))
        self.assertTrue(any(e['phase'] == 'upload' and 0 < e['percent'] < 100 for e in events))
        self.assertTrue(all(set(e) == {'phase', 'completed_bytes', 'total_bytes', 'percent'} for e in events))
        for phase in ('upload', 'verify'):
            counts = [e['completed_bytes'] for e in events if e['phase'] == phase]
            self.assertEqual(counts, sorted(counts))


if __name__ == '__main__':
    unittest.main()
