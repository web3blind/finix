"""Native subprocess tests; synthetic provider only, never credentials or cloud."""
import os
from pathlib import Path
import tempfile
import time
import unittest

from finix.workspace_drive import WorkspaceRunner


class NativeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.root = Path(self.tmp.name)
        if os.name == 'nt':
            import shutil
            from finix import windows_native as win
            self.tmp.cleanup()
            win.private_dir(self.root)
            self.addCleanup(shutil.rmtree, self.root)
        else:
            self.addCleanup(self.tmp.cleanup)
        self.script = self.root / 'provider.py'



class NativeWorkspaceTests(NativeFixture):
    def test_child_environment_isolated_and_stdin_disconnected(self):
        from unittest.mock import patch
        import subprocess
        self.script.write_text('import os,sys,json\ndef main():\n print(json.dumps({"isolated":sys.flags.isolated,"leaks":[k for k in ("GOOGLE_APPLICATION_CREDENTIALS","PYTHONPATH","HERMES_GWS_BIN","FINIX_TEST_SECRET") if k in os.environ]}))\n')
        with patch.dict(os.environ, {'GOOGLE_APPLICATION_CREDENTIALS':'synthetic', 'PYTHONPATH':'synthetic',
                                     'HERMES_GWS_BIN':'synthetic', 'FINIX_TEST_SECRET':'synthetic'}), patch('subprocess.Popen', wraps=subprocess.Popen) as spawn:
            result = WorkspaceRunner(self.root, script=self.script)('probe')
        self.assertEqual(result, {'isolated': 1, 'leaks': []})
        self.assertEqual(spawn.call_args.kwargs.get('stdin'), subprocess.DEVNULL)

    def test_adapter_backup_download_and_retention(self):
        from finix.workspace_drive import WorkspaceDrive
        from finix import core
        from tests.test_workspace_drive import FakeDrive
        cipher = self.root / 'input.age'
        cipher.write_bytes(b'age-encryption.org/v1\nsynthetic-ciphertext\n')
        completion = {'sha256': core.digest_file(cipher), 'complete': True, 'omitted_count': 0}
        provider = FakeDrive()
        drive = WorkspaceDrive(self.root, 'a' * 64, self.root / 'state', runner=provider)
        first = drive.backup_cipher(cipher, completion=completion)
        latest = drive.backup_cipher(cipher, completion=completion, keep=1)
        self.assertTrue(provider.objects[first['id']]['trashed'])
        recovered = self.root / 'output.age'
        drive.download(latest, recovered)
        self.assertEqual(recovered.read_bytes(), cipher.read_bytes())

    def test_native_helper_startup_and_profile(self):
        self.script.write_text('import json,os\ndef main():\n print(json.dumps({"home":os.environ["HERMES_HOME"]}))\n')
        self.assertEqual(WorkspaceRunner(self.root, script=self.script)('probe'), {'home': str(self.root)})


class InterpreterContextTests(NativeFixture):
    def test_status_rejects_parent_only_dependencies_without_loading_provider(self):
        import sys, subprocess, importlib.util
        from unittest.mock import patch
        from finix.workspace_drive import WorkspaceDrive
        venv = self.root / 'empty-venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
        binary = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        ambient = self.root / 'ambient'
        for name in ('google/__init__.py', 'google/auth/__init__.py',
                     'google/auth/transport/__init__.py', 'google/auth/transport/requests.py',
                     'googleapiclient/__init__.py', 'googleapiclient/http.py', 'requests.py'):
            path = ambient / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('')
        self.script.write_text('raise AssertionError("status must not load provider or login")\n')
        (self.root / 'google_token.json').write_text('synthetic, presence only')
        modules = {k: v for k, v in sys.modules.items() if not k.startswith(('google', 'requests'))}
        with patch.dict(sys.modules, modules, clear=True), patch.object(sys, 'path', [str(ambient), *sys.path]), patch.dict(os.environ, {'PYTHONPATH': str(ambient)}), patch('sys.executable', str(binary)), patch('finix.workspace_drive.helper_path', return_value=self.script):
            self.assertTrue(all(importlib.util.find_spec(name) for name in
                ('google.auth', 'google.auth.transport.requests', 'googleapiclient.http', 'requests')))
            status = WorkspaceDrive(self.root, 'a' * 64, self.root / 'state').status()
        self.assertFalse(status['ready'])
        self.assertEqual(status['code'], 'DEPENDENCIES_MISSING')
        self.assertFalse(status['live_google_verified'])

    def test_status_accepts_only_child_venv_dependencies_without_provider_import(self):
        import sys, subprocess
        from unittest.mock import patch
        from finix.workspace_drive import WorkspaceDrive
        venv = self.root / 'ready-venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
        binary = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        site = venv / 'Lib/site-packages' if os.name == 'nt' else next((venv / 'lib').glob('python*/site-packages'))
        for name in ('google/__init__.py', 'google/auth/__init__.py',
                     'google/auth/transport/__init__.py', 'google/auth/transport/requests.py',
                     'googleapiclient/__init__.py', 'googleapiclient/http.py', 'requests.py'):
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('')
        self.script.write_text('raise AssertionError("must not load provider")\n')
        with patch('sys.executable', str(binary)), patch('finix.workspace_drive.helper_path', return_value=self.script):
            drive = WorkspaceDrive(self.root, 'a' * 64, self.root / 'state')
            self.assertEqual(drive.status()['code'], 'TOKEN_ABSENT')
            (self.root / 'google_token.json').write_text('invalid synthetic token, must not parse')
            self.assertTrue(drive.status()['ready'])
            (site / 'requests.py').unlink()
            self.assertEqual(drive.status()['code'], 'DEPENDENCIES_MISSING')

    @unittest.skipUnless(os.name == 'posix', 'Linux symlink virtualenv regression')
    def test_fresh_venv_helper_preserves_prefix_and_local_dependency(self):
        import sys, subprocess
        from unittest.mock import patch
        venv = self.root / 'venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
        binary = venv / 'bin/python'
        site = next((venv / 'lib').glob('python*/site-packages'))
        (site / 'finix_venv_only.py').write_text('VALUE = "venv-only"\n')
        self.script.write_text('import sys,json,finix_venv_only\ndef main():\n print(json.dumps({"prefix":sys.prefix,"dependency":finix_venv_only.VALUE}))\n')
        with patch('sys.executable', str(binary)):
            result = WorkspaceRunner(self.root, script=self.script)('probe')
        self.assertEqual(result, {'prefix': str(venv), 'dependency': 'venv-only'})


class CancellationTests(NativeFixture):
    @unittest.skipUnless(os.name == 'nt', 'Native Windows pipe cancellation observer')
    def test_windows_cancellation_predicate_is_latched(self):
        from finix import core
        self.script.write_text('import time\ndef main():\n time.sleep(3)\n')
        started, consumed = time.monotonic(), []
        def cancel():
            if time.monotonic() - started > .3 and not consumed:
                consumed.append(True)
                return True
            return False
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            WorkspaceRunner(self.root, script=self.script)('probe', cancel=cancel)

    def test_cancel_after_pipes_close(self):
        import threading
        from finix import core
        self.script.write_text('import os,time\ndef main():\n os.close(1)\n os.close(2)\n time.sleep(3)\n')
        cancel = threading.Event()
        timer = threading.Timer(.4, cancel.set)
        timer.start()
        self.addCleanup(timer.join)
        start = time.monotonic()
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            WorkspaceRunner(self.root, script=self.script, timeout=5)('probe', cancel=cancel)
        self.assertLess(time.monotonic() - start, 2)

    def test_cancel_silent_child(self):
        import threading
        from finix import core
        self.script.write_text('import time\ndef main():\n time.sleep(30)\n')
        cancel = threading.Event()
        timer = threading.Timer(.3, cancel.set)
        timer.start()
        self.addCleanup(timer.join)
        start = time.monotonic()
        with self.assertRaisesRegex(core.FinixError, 'cancelled'):
            WorkspaceRunner(self.root, script=self.script)('probe', cancel=cancel)
        self.assertLess(time.monotonic() - start, 3)


class TrustTests(NativeFixture):
    @unittest.skipUnless(os.name == 'posix', 'Linux interpreter symlink-chain trust')
    def test_interpreter_symlink_hops_require_trusted_directory_chains(self):
        import sys
        from unittest.mock import patch
        from finix.workspace_drive import WorkspaceError
        intermediate = self.root / 'intermediate'
        intermediate.mkdir()
        (intermediate / 'python').symlink_to(Path(sys.executable).resolve())
        invocation = self.root / 'python'
        invocation.symlink_to('intermediate/python')
        self.script.write_text('def main():\n print("{}")\n')
        with patch('sys.executable', str(invocation)):
            self.assertEqual(WorkspaceRunner(self.root, script=self.script)('probe'), {})
            intermediate.chmod(0o777)
            try:
                with self.assertRaisesRegex(WorkspaceError, 'UNTRUSTED_HELPER'):
                    WorkspaceRunner(self.root, script=self.script)('probe')
            finally:
                intermediate.chmod(0o700)

    def test_missing_platform_primitive_is_not_ready(self):
        from unittest.mock import patch
        from finix.workspace_drive import WorkspaceDrive, WorkspaceError
        self.script.write_text('def main():\n print("{}")\n')
        (self.root / 'google_token.json').write_text('synthetic')
        if os.name == 'nt':
            from finix import windows_native as backend
            attribute = 'output_events'
        else:
            import resource as backend
            attribute = 'setrlimit'
        with patch.object(backend, attribute, None), patch('finix.workspace_drive.helper_path', return_value=self.script), patch('importlib.util.find_spec', return_value=object()):
            self.assertEqual(WorkspaceDrive(self.root, 'a' * 64, self.root / 'state').status()['code'], 'PLATFORM_UNAVAILABLE')
            with self.assertRaisesRegex(WorkspaceError, 'PLATFORM_UNAVAILABLE'):
                WorkspaceRunner(self.root, script=self.script)('probe')

    def test_status_dependency_probe_is_bounded_without_loading_provider(self):
        from unittest.mock import patch
        from finix.workspace_drive import WorkspaceDrive
        self.script.write_text('raise AssertionError("must not load provider")\n')
        package = self.root / 'google'
        package.mkdir()
        init = package / '__init__.py'
        for source, expected in (('import os;os.write(2,b"x"*70000)', 'OUTPUT_LIMIT'),
                                 ('import time;time.sleep(10)', 'TIMEOUT')):
            init.write_text(source)
            started = time.monotonic()
            with patch('finix.workspace_drive.helper_path', return_value=self.script):
                status = WorkspaceDrive(self.root, 'a' * 64, self.root / 'state').status()
            self.assertFalse(status['ready'])
            self.assertEqual(status['code'], expected)
            self.assertLess(time.monotonic() - started, 8)

    def test_missing_absolute_interpreter_is_not_ready(self):
        from unittest.mock import patch
        from finix.workspace_drive import WorkspaceDrive, WorkspaceError
        self.script.write_text('def main():\n print("{}")\n')
        (self.root / 'google_token.json').write_text('synthetic')
        missing = str(self.root / 'missing-python.exe')
        with patch('sys.executable', missing), patch('finix.workspace_drive.helper_path', return_value=self.script), patch('importlib.util.find_spec', return_value=object()):
            self.assertEqual(WorkspaceDrive(self.root, 'a' * 64, self.root / 'state').status()['code'], 'UNTRUSTED_HELPER')
            with self.assertRaisesRegex(WorkspaceError, 'UNTRUSTED_HELPER'):
                WorkspaceRunner(self.root, script=self.script)('probe')

    def test_relative_interpreter_never_searched_and_not_ready(self):
        from unittest.mock import patch
        from finix.workspace_drive import WorkspaceDrive, WorkspaceError
        self.script.write_text('def main():\n print("{}")\n')
        (self.root / 'google_token.json').write_text('synthetic, do not read')
        with patch('sys.executable', 'python'), patch('finix.workspace_drive.helper_path', return_value=self.script), patch('importlib.util.find_spec', return_value=object()):
            with self.assertRaisesRegex(WorkspaceError, 'UNTRUSTED_HELPER'):
                WorkspaceRunner(self.root, script=self.script)('probe')
            status = WorkspaceDrive(self.root, 'a' * 64, self.root / 'state').status()
            self.assertFalse(status['ready'])
            self.assertEqual(status['code'], 'UNTRUSTED_HELPER')


class DownloadBoundaryTests(unittest.TestCase):
    def test_range_overflow_never_writes_excess_and_disables_env(self):
        from finix.workspace_helper import _download
        from types import SimpleNamespace
        class Response:
            status_code = 206
            headers = {'Content-Range': 'bytes 0-2/3', 'Content-Length': '3'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, chunk_size):
                yield b'ab'
                yield b'cd'
        class Session:
            trust_env = True
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, url, **kwargs):
                self.kwargs = kwargs
                return Response()
        session = Session()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            output = Path(root) / 'cipher'
            with self.assertRaisesRegex(ValueError, 'overflow'):
                _download(SimpleNamespace(get_credentials=lambda: object()), 'file', output, 3,
                          session_factory=lambda credentials: session)
            self.assertEqual(output.read_bytes(), b'ab')
            self.assertFalse(session.trust_env)
            self.assertFalse(session.kwargs['allow_redirects'])
            self.assertTrue(session.kwargs['stream'])


class EnvironmentTests(unittest.TestCase):
    def test_windows_os_environment_without_secret_overlay(self):
        from unittest.mock import patch
        from finix.workspace_drive import child_env
        before = dict(os.environ)
        with patch.dict(os.environ, {'SystemRoot': 'C:/Windows', 'USERPROFILE': 'C:/Synthetic',
                                    'GOOGLE_APPLICATION_CREDENTIALS': 'do-not-copy', 'PYTHONPATH': 'do-not-copy'}):
            snapshot = dict(os.environ)
            result = child_env(Path('synthetic'))
            self.assertEqual(dict(os.environ), snapshot)
            self.assertNotIn('PYTHONPATH', result)
            self.assertNotIn('GOOGLE_APPLICATION_CREDENTIALS', result)
            if os.name == 'nt':
                self.assertEqual(result.get('SystemRoot'), 'C:/Windows')
        self.assertEqual(dict(os.environ), before)


class CliTests(unittest.TestCase):
    def test_record_uses_native_regular_input(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        import io, json
        from finix.workspace_drive import main
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            record = Path(root) / 'record.json'
            output = Path(root) / 'cipher.age'
            row = {'sha256': 'a' * 64}
            record.write_text(json.dumps(row))
            def download(value, destination):
                self.assertEqual(value, row)
                Path(destination).write_bytes(b'synthetic')
            with patch('finix.workspace_drive.WorkspaceDrive', return_value=SimpleNamespace(download=download)), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['download', '--home', root, '--profile-id', 'a' * 64,
                                       '--record', str(record), '--output', str(output)]), 0)
            self.assertEqual(output.read_bytes(), b'synthetic')


class LimitsTests(NativeFixture):
    def test_timeout_silent_and_closed_pipes(self):
        from finix.workspace_drive import WorkspaceError
        for close in ('', 'os.close(1);os.close(2);'):
            with self.subTest(close=bool(close)):
                self.script.write_text('import os,time\ndef main():\n ' + close + 'time.sleep(3)\n')
                start = time.monotonic()
                with self.assertRaisesRegex(WorkspaceError, 'TIMEOUT'):
                    WorkspaceRunner(self.root, script=self.script, timeout=.3)('probe')
                self.assertLess(time.monotonic() - start, 2)

    def test_stdout_stderr_and_combined_bounds(self):
        from finix.workspace_drive import WorkspaceError
        for body in ("os.write(1,b'x'*70000)", "os.write(2,b'x'*70000)",
                     "os.write(1,b'x'*33000);os.write(2,b'x'*33000)"):
            with self.subTest(body=body):
                self.script.write_text('import os\ndef main():\n ' + body + '\n')
                with self.assertRaisesRegex(WorkspaceError, 'OUTPUT_LIMIT'):
                    WorkspaceRunner(self.root, script=self.script, timeout=2)('probe')

    def test_fatal_startup_and_invalid_or_nonzero_responses(self):
        from finix.workspace_drive import WorkspaceError
        for source in ('raise RuntimeError("synthetic private diagnostic")',
                       'import os;os._exit(7)', 'def main():\n print("not JSON")\n',
                       'import os\ndef main():\n print("{}",flush=True)\n os._exit(7)\n'):
            self.script.write_text(source)
            with self.assertRaises(WorkspaceError) as caught:
                WorkspaceRunner(self.root, script=self.script, timeout=2)('probe')
            self.assertNotIn('synthetic private diagnostic', str(caught.exception))

    @unittest.skipUnless(os.name == 'posix', 'Linux kernel RLIMIT assertion')
    def test_linux_file_and_core_limits(self):
        target = self.root / 'bounded'
        self.script.write_text('import resource,json\ndef main():\n '
            'print(json.dumps({"core":resource.getrlimit(resource.RLIMIT_CORE),"file":resource.getrlimit(resource.RLIMIT_FSIZE)}))\n')
        result = WorkspaceRunner(self.root, script=self.script, max_bytes=1024)('probe')
        self.assertEqual(result, {'core': [0, 0], 'file': [1024, 1024]})
        self.script.write_text('def main():\n open(' + repr(str(target)) + ',"wb").write(b"x"*8192)\n')
        from finix.workspace_drive import WorkspaceError
        with self.assertRaises(WorkspaceError):
            WorkspaceRunner(self.root, script=self.script, max_bytes=1024)('probe')
        self.assertLessEqual(target.stat().st_size, 1024)

    def test_descendant_killed_on_timeout_and_cancel(self):
        import threading
        from finix import core
        for cancellation in (False, True):
            with self.subTest(cancellation=cancellation):
                marker = self.root / 'descendant'
                child = 'import time,pathlib;time.sleep(1.1);pathlib.Path(' + repr(str(marker)) + ').write_text("survived")'
                self.script.write_text('import subprocess,sys,time\ndef main():\n '
                    'subprocess.Popen([sys.executable,"-I","-c",' + repr(child) + '])\n time.sleep(3)\n')
                cancel = threading.Event()
                timer = threading.Timer(.4, cancel.set)
                if cancellation: timer.start()
                try:
                    with self.assertRaisesRegex(core.FinixError, 'cancelled' if cancellation else 'TIMEOUT'):
                        WorkspaceRunner(self.root, script=self.script, timeout=2 if cancellation else .4)('probe', cancel=cancel)
                finally:
                    if cancellation: timer.join()
                time.sleep(1.2)
                self.assertFalse(marker.exists(), 'descendant escaped containment')

    def test_synthetic_sdk_metadata_boundary_and_error_codes(self):
        from finix.workspace_drive import WorkspaceError
        self.script.write_text('class Service:\n def files(self): return self\n def get(self,**kw):\n  assert kw["fileId"] == "file"\n  return self\n def execute(self): return {"id":"file","trashed":False}\ndef build_service(*args):\n assert _gws_binary() is None\n return Service()\n')
        self.assertEqual(WorkspaceRunner(self.root, script=self.script)('metadata', 'file'), {'id': 'file', 'trashed': False})
        for status, expected in ((403, 'PERMISSION'), (404, 'NOT_FOUND'), (500, 'PROVIDER_ERROR')):
            self.script.write_text('class Failure(Exception):\n resp=type("Response",(),{"status":' + str(status) + '})()\ndef build_service(*args): raise Failure("do not disclose")\n')
            with self.assertRaisesRegex(WorkspaceError, expected):
                WorkspaceRunner(self.root, script=self.script)('metadata', 'file')


if __name__ == '__main__':
    unittest.main()
