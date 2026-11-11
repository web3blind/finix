"""Offline restart/fault contracts: no provider binary or profile discovery."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finix import core, cloud_files
from finix.rclone_storage import RcloneStorage, RcloneError


class Transport:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fault = None
        self.before_copy = None

    def __call__(self, op, *args, output=None, expected_size=None):
        self.calls.append((op, args))
        if op == 'listremotes':
            return b'[{"name":"cloud","type":"yandex","source":"file"}]'
        if op == 'copyto':
            if self.before_copy:
                self.before_copy()
            data = Path(args[0]).read_bytes()
            if self.fault == 'before':
                raise RcloneError('TIMEOUT')
            self.objects[args[1]] = data[:25] if self.fault == 'mid' else data
            if self.fault in {'mid', 'after'}:
                raise RcloneError('TIMEOUT')
            return b''
        if op == 'lsjson':
            if '--stat' not in args:
                return json.dumps([{'Name': p.rsplit('/', 1)[1], 'Size': len(v), 'IsDir': False}
                    for p, v in self.objects.items() if p.startswith(args[0] + '/')]).encode()
            if args[0] not in self.objects:
                raise RcloneError('NOT_FOUND')
            return json.dumps({'Name': args[0].rsplit('/', 1)[1], 'Size': len(self.objects[args[0]]), 'IsDir': False}).encode()
        if op == 'cat':
            output.write(self.objects[args[0]])
            return b''
        raise AssertionError('No delete or unscoped operation permitted: ' + op)


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = core.private_dir(Path(self.temp.name) / 'private')
        self.auth = core.private_dir(self.root / 'auth')
        self.config = self.auth / 'fixture.conf'
        with core.regular_output(self.config) as fd:
            os.write(fd, b'[cloud]\ntype=yandex\n')
        self.state = self.root / 'state'
        self.cipher = self.root / 'input.age'
        with core.regular_output(self.cipher) as fd:
            os.write(fd, b'age-encryption.org/v1\n' + b'fixture' * 100)
        self.transport = Transport()
        self.drive = self.adapter()
        self.completion = dict(sha256=hashlib.sha256(self.cipher.read_bytes()).hexdigest(), complete=True, omitted_count=0)

    def adapter(self):
        return RcloneStorage(self.root, 'a' * 64, self.state, str(self.config), 'cloud', 'Finix', runner=self.transport)

    def pending(self):
        return json.loads((self.state / 'rclone-pending.json').read_text())

    def test_durable_intent_before_upload_restart_uses_same_identity(self):
        seen = []
        def capture():
            self.assertTrue((self.state / 'rclone-pending.json').is_file(), 'durable intent missing before upload')
            seen.append(self.pending())
        self.transport.before_copy = capture
        self.transport.fault = 'before'
        with self.assertRaises(RcloneError):
            self.drive.backup_cipher(self.cipher, completion=self.completion)
        intent = self.pending()
        self.assertEqual(intent['record']['completion'], self.completion)
        self.assertNotIn(str(self.config), json.dumps(intent))
        self.assertEqual(seen[0], intent)
        self.assertEqual(json.loads((self.state / 'rclone.json').read_text())['records'], [])
        self.transport.fault = None
        row = self.adapter().reconcile(self.cipher)
        self.assertEqual(row['id'], intent['record']['id'])
        self.assertTrue(row['verified'])
        self.assertEqual(len(self.transport.objects), 1)
        self.assertFalse((self.state / 'rclone-pending.json').exists())
        self.assertEqual(len(json.loads((self.state / 'rclone.json').read_text())['records']), 1)

    def test_intent_and_ledger_are_private_and_directory_durable(self):
        fsync = os.fsync
        synced = []
        def flush(fd):
            import stat
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return fsync(fd)
        with patch.object(os, 'fsync', side_effect=flush):
            self.drive.backup_cipher(self.cipher, completion=self.completion)
        if os.name != 'nt':
            self.assertGreaterEqual(synced.count(True), 3, 'intent, ledger and cleanup need directory fsync')
            self.assertEqual((self.state / 'rclone.json').stat().st_mode & 0o777, 0o600)
        else:
            from finix import windows_native as win
            self.assertTrue(win.trusted_directory(self.state, private=True))

    def test_legacy_scans_only_namespace_hashes_candidates_and_explicit_adoption(self):
        core.private_dir(self.state)
        with self.drive._locked():
            journal = self.drive.ledger()
        base = 'cloud:Finix/' + journal['namespace']
        good = 'c' * 32 + '.age'
        wrong = 'd' * 32 + '.age'
        self.transport.objects[base + '/' + good] = self.cipher.read_bytes()
        self.transport.objects[base + '/' + wrong] = self.cipher.read_bytes()[:-1] + b'X'
        self.transport.objects['cloud:Finix/foreign/' + good] = self.cipher.read_bytes()
        self.assertTrue(callable(getattr(self.drive, 'reconcile_legacy', None)), 'missing bounded legacy reconciliation')
        result = self.drive.reconcile_legacy(self.cipher)
        self.assertEqual([r['id'] for r in result], [good])
        self.assertFalse((self.state / 'rclone-pending.json').exists())
        self.assertEqual(json.loads((self.state / 'rclone.json').read_text())['records'], [])
        listings = [args for op, args in self.transport.calls if op == 'lsjson' and '--stat' not in args]
        self.assertEqual(listings[0], (base, '--files-only', '--max-depth', '1'))
        row = self.drive.reconcile_legacy(self.cipher, object_id=good)
        self.assertTrue(row['verified'])
        self.assertIsNone(row['completion'])
        self.assertEqual(cloud_files.retention_candidates([row, dict(row, id='e'*32+'.age')], 1), [])
        self.assertFalse(any(op in {'copyto', 'deletefile'} for op, _ in self.transport.calls))

    def test_profile_retry_does_not_build_another_snapshot(self):
        from finix.integration import Profile
        self.transport.fault = 'after'
        with self.assertRaises(RcloneError):
            self.drive.backup_cipher(self.cipher, completion=self.completion)
        profile = Profile(core.private_dir(self.root / 'profile-fixture'))
        cfg = dict(storage='rclone', recipient='public-fixture-only', interval_hours=0, next_run=0, retention=1)
        with patch.object(Profile, 'config', return_value=cfg), patch.object(Profile, 'rclone', return_value=self.drive):
            with patch.object(core, 'backup', side_effect=AssertionError('must not create another snapshot')):
                with self.assertRaisesRegex(RcloneError, 'PENDING_RECONCILIATION'):
                    profile.backup()

    def test_diagnostics_are_closed_numeric_and_actionable(self):
        from finix import rclone_storage as storage
        from finix.setup_errors import setup_error_code
        self.assertTrue(callable(getattr(storage, 'failure_code', None)), 'missing safe diagnostic classifier')
        for status, code in ((401, 'AUTH_REQUIRED'), (403, 'ACCESS_DENIED'), (507, 'QUOTA_EXCEEDED'), (408, 'TIMEOUT'), (429, 'RATE_LIMITED')):
            raw = json.dumps({'level': 'error', 'status': status, 'msg': 'private token path secret'}).encode()
            self.assertEqual(storage.failure_code(1, raw), code)
            error = RcloneError(code)
            self.assertNotIn('private', str(error))
            self.assertNotEqual(setup_error_code(error), 'SETUP_CHECK_FAILED')
        for raw in (b'{"msg":"401 token expired quota exceeded 507"}', b'{"status":"401"}', b'{"status":true}', b'not-json secret', b'x'*65537):
            self.assertEqual(storage.failure_code(1, raw), 'PROVIDER_ERROR')
        import errno
        self.assertEqual(storage.failure_code(1, json.dumps({'errno': errno.ECONNREFUSED}).encode()), 'CONNECTION_FAILED')
        self.assertEqual(storage.failure_code(10, b''), 'TIMEOUT')
        self.assertEqual(storage.failure_code(1, b'{"status":401}\n{"status":507}'), 'PROVIDER_ERROR')
        self.assertNotIn('secret', str(RcloneError('secret untrusted payload')))
        self.assertIn('same ciphertext', str(RcloneError('PENDING_RECONCILIATION')))

    def test_authorized_parent_cli_reconciles_without_snapshot_or_secret_output(self):
        from finix.rclone_storage import main
        import io
        from contextlib import redirect_stdout, redirect_stderr
        self.transport.fault = 'after'
        with self.assertRaises(RcloneError):
            self.drive.backup_cipher(self.cipher, completion=self.completion)
        args = ['reconcile', '--home', str(self.root), '--profile-id', 'a'*64,
                '--state', str(self.state), '--config', str(self.config), '--remote', 'cloud',
                '--root', 'Finix', '--cipher', str(self.cipher)]
        out, err = io.StringIO(), io.StringIO()
        with patch('finix.rclone_storage.RcloneStorage', return_value=self.drive), redirect_stdout(out), redirect_stderr(err):
            try:
                result = main(args)
            except SystemExit:
                self.fail('CLI reconcile command is missing')
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(out.getvalue())['verified'])
        self.assertNotIn(str(self.config), out.getvalue() + err.getvalue())

    def test_public_ledger_and_prune_preserve_locked_restart_compatibility(self):
        self.drive.backup_cipher(self.cipher, completion=self.completion)
        try:
            journal = self.adapter().ledger()
        except AttributeError:
            self.fail('public ledger read lost its private state anchor')
        self.assertEqual(len(journal['records']), 1)
        self.adapter().prune(journal, 1)

    def test_same_adapter_prune_cannot_delete_under_other_thread_or_resurrect_stale_ledger(self):
        import threading
        old = self.drive.backup_cipher(self.cipher, completion=self.completion)
        self.drive.backup_cipher(self.cipher, completion=self.completion)
        stale = self.drive.ledger()
        original = self.transport
        def deleting(op, *args, **kwargs):
            if op == 'deletefile':
                original.calls.append((op, args))
                original.objects.pop(args[0])
                return b''
            return original(op, *args, **kwargs)
        self.drive.runner = deleting
        loaded, resume, entered, finished = (threading.Event() for _ in range(4))
        errors, prune_errors = [], []
        intent = self.drive._intent
        def paused(journal, row):
            loaded.set()
            if not resume.wait(10):
                raise TimeoutError('backup barrier')
            return intent(journal, row)
        def backup():
            try:
                self.drive.backup_cipher(self.cipher, completion=self.completion)
            except BaseException as exc:
                errors.append(exc)
        def prune():
            entered.set()
            try:
                self.drive.prune(stale, 1)
            except BaseException as exc:
                prune_errors.append(exc)
            finally:
                finished.set()
        with patch.object(self.drive, '_intent', side_effect=paused):
            worker = threading.Thread(target=backup)
            worker.start()
            try:
                self.assertTrue(loaded.wait(5))
                contender = threading.Thread(target=prune)
                contender.start()
                self.assertTrue(entered.wait(5))
                # A contender may reject immediately or serialize, never delete.
                finished.wait(.2)
                deleted_under_lock = self.drive.object_path(old) not in original.objects
            finally:
                resume.set()
                worker.join(5)
                if 'contender' in locals():
                    contender.join(5)
        self.assertFalse(worker.is_alive(), 'nested same-thread lock deadlocked')
        self.assertFalse(contender.is_alive(), 'contender did not finish')
        self.assertEqual(errors, [])
        self.assertFalse(deleted_under_lock, 'prune deleted while another thread owned the anchor')
        self.assertEqual(len(prune_errors), 1, 'stale ledger must be rejected after serialization')
        self.assertIsInstance(prune_errors[0], core.FinixError)
        journal = self.drive.ledger()
        self.assertEqual(len(journal['records']), 3)
        self.assertTrue(all(self.drive.object_path(row) in original.objects for row in journal['records']),
                        'backup resurrected a deleted ledger record')
        self.assertFalse(any(op == 'deletefile' for op, _ in original.calls))

    def test_real_subprocess_diagnostic_stream_is_bounded_and_never_printed(self):
        import sys
        import subprocess
        from finix.rclone_storage import RcloneRunner
        fixture = self.root / 'fixture.py'
        fixture.write_text('import sys\nsys.stderr.write(\'{"status":401,"msg":"private-secret"}\\n\')\nsys.exit(1)\n')
        if os.name == 'nt':
            from finix import windows_native as win
            original = win.popen
            target = 'finix.windows_native.popen'
        else:
            original = subprocess.Popen
            target = 'finix.rclone_storage.subprocess.Popen'
        binary = self.root / 'offline-fixture.exe'
        with core.regular_output(binary) as fd:
            os.write(fd, b'fixture sentinel: intercepted before execution')
        binary.chmod(0o700)
        def launch(argv, **kwargs):
            self.assertEqual(argv[0], str(binary))
            self.assertIn('--use-json-log', argv)
            self.assertIn('--ask-password=false', argv)
            return original([sys.executable, str(fixture)], **kwargs)
        runner = RcloneRunner(self.root, self.config, binary=str(binary))
        with patch(target, side_effect=launch):
            with self.assertRaisesRegex(RcloneError, 'AUTH_REQUIRED') as caught:
                if os.name == 'nt':
                    # Test the native pipe backend, not executable discovery:
                    # the owning cache ancestor is intentionally not a tool install.
                    runner._windows_call([str(binary), '--use-json-log', '--ask-password=false'], 'listremotes', None, None)
                else:
                    runner('listremotes', '--json')
        self.assertNotIn('private-secret', str(caught.exception))

    def test_reconciled_incomplete_and_unknown_never_authorize_retention(self):
        for complete in (False, None):
            self.state = self.root / ('protected-' + str(complete))
            self.transport.fault = None
            drive = self.adapter()
            first = drive.backup_cipher(self.cipher, completion=self.completion)
            provenance = dict(self.completion, complete=False, omitted_count=1) if complete is False else None
            self.transport.fault = 'after'
            with self.assertRaises(RcloneError):
                drive.backup_cipher(self.cipher, keep=1, completion=provenance)
            self.assertIn(drive.object_path(first), self.transport.objects)
            recovered = self.adapter().reconcile()
            self.assertEqual(recovered['completion'], provenance)
            journal = self.adapter().ledger()
            self.adapter().prune(journal, 1)
            self.assertEqual(len(self.adapter().ledger()['records']), 2)
            self.assertFalse(any(op == 'deletefile' for op, _ in self.transport.calls))

    def test_legacy_bounds_and_missing_namespace_fail_closed(self):
        self.assertTrue(callable(getattr(self.drive, 'reconcile_legacy', None)))
        with self.assertRaisesRegex(RcloneError, 'LEGACY_NAMESPACE_REQUIRED'):
            self.drive.reconcile_legacy(self.cipher)
        self.drive.ledger()
        original = self.transport
        def oversized(op, *args, **kwargs):
            if op == 'lsjson' and '--stat' not in args:
                return b'[' + b' ' * 65536 + b']'
            return original(op, *args, **kwargs)
        self.drive.runner = oversized
        with self.assertRaisesRegex(RcloneError, 'OUTPUT_LIMIT'):
            self.drive.reconcile_legacy(self.cipher)
        self.assertEqual(self.drive.ledger()['records'], [])
        self.assertFalse(self.transport.objects)

    def test_abrupt_process_exit_after_upload_restarts_without_second_copy(self):
        import subprocess
        import sys
        worker = self.root / 'crash-worker.py'
        worker.write_text('''import os, sys
from pathlib import Path
from finix.rclone_storage import RcloneStorage, RcloneError
root = Path(sys.argv[1])
def run(op, *args, **kwargs):
    if op == 'listremotes':
        return b'[{"name":"cloud","type":"yandex","source":"file"}]'
    if op == 'lsjson':
        raise RcloneError('NOT_FOUND')
    if op == 'copyto':
        with (root / 'uploaded.age').open('wb') as out:
            out.write(Path(args[0]).read_bytes())
            out.flush()
            os.fsync(out.fileno())
        os._exit(73)
    raise AssertionError(op)
drive = RcloneStorage(root, 'a'*64, root/'state', str(root/'auth'/'fixture.conf'), 'cloud', 'Finix', runner=run)
drive.backup_cipher(root/'input.age')
''')
        process = subprocess.run([sys.executable, str(worker), str(self.root)],
            cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])), capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 73, 'isolated worker did not reach injected exit')
        row = self.pending()['record']
        self.transport.objects[self.drive.object_path(row)] = (self.root / 'uploaded.age').read_bytes()
        result = self.adapter().reconcile()
        self.assertEqual(result['id'], row['id'])
        self.assertFalse(any(op == 'copyto' for op, _ in self.transport.calls))

    def test_reconcile_without_original_ledger_never_invents_namespace(self):
        with self.assertRaises(core.FinixError):
            self.drive.reconcile(self.cipher)
        self.assertFalse((self.state / 'rclone.json').exists(), 'reconciliation invented a new namespace')
        self.assertFalse(self.transport.calls)

    def test_pending_blocks_new_identity_and_survives_all_crash_windows(self):
        for phase in ('before', 'mid', 'after', 'commit', 'cleanup'):
            with self.subTest(phase=phase):
                self.state = self.root / ('state-' + phase)
                drive = self.adapter()
                self.transport.fault = phase
                original = drive._write
                def write(name, data):
                    if phase == 'commit' and name == 'rclone.json' and data['records']:
                        raise OSError('injected power loss before commit')
                    original(name, data)
                original_clear = drive._clear_pending
                def clear():
                    if phase == 'cleanup':
                        raise OSError('injected power loss after commit')
                    original_clear()
                with patch.object(drive, '_write', side_effect=write), patch.object(drive, '_clear_pending', side_effect=clear):
                    with self.assertRaises((RcloneError, OSError)):
                        drive.backup_cipher(self.cipher, keep=1, completion=self.completion)
                row = self.pending()['record']
                self.transport.fault = None
                with self.assertRaisesRegex(RcloneError, 'PENDING_RECONCILIATION'):
                    self.adapter().backup_cipher(self.cipher, completion=self.completion)
                if phase == 'mid':
                    before = dict(self.transport.objects)
                    with self.assertRaises(core.FinixError):
                        self.adapter().reconcile(self.cipher)
                    self.assertEqual(self.transport.objects, before)
                    self.assertTrue((self.state / 'rclone-pending.json').exists())
                    continue
                result = self.adapter().reconcile(self.cipher)
                self.assertEqual(result['id'], row['id'])
                records = json.loads((self.state / 'rclone.json').read_text())['records']
                self.assertEqual([r['id'] for r in records], [row['id']])
                copies = [args[1] for op, args in self.transport.calls if op == 'copyto' and args[1] == drive.object_path(row)]
                self.assertEqual(len(copies), 2 if phase == 'before' else 1)

    def test_wrong_hash_and_foreign_binding_cannot_commit_or_overwrite(self):
        self.transport.fault = 'after'
        with self.assertRaises(RcloneError):
            self.drive.backup_cipher(self.cipher)
        row = self.pending()['record']
        object_path = self.drive.object_path(row)
        self.transport.objects[object_path] = self.cipher.read_bytes()[:-1] + b'X'
        with self.assertRaises(core.FinixError):
            self.adapter().reconcile(self.cipher)
        self.assertEqual(json.loads((self.state / 'rclone.json').read_text())['records'], [])
        intent = self.pending()
        intent['record']['namespace'] = 'b' * 32
        core.atomic_json(self.state / 'rclone-pending.json', intent)
        calls = len(self.transport.calls)
        with self.assertRaises(core.FinixError):
            self.adapter().reconcile(self.cipher)
        self.assertTrue(all(op == 'listremotes' for op, _ in self.transport.calls[calls:]))


if __name__ == '__main__':
    unittest.main()
