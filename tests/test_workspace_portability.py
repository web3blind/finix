"""Public managed-provider boundaries; the credential-owning subprocess is removed.

Its stdout, dependency lookup, RLIMIT and descendant tests are replaced by a
zero-subprocess assertion plus exact HTTP byte bounds/cancellation tests.
"""
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from finix import core
from finix.workspace_drive import WorkspaceRunner, WorkspaceDrive, WorkspaceError

class NativeFixture(unittest.TestCase):
    """Private synthetic fixture retained for non-provider CLI worker tests."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.root=core.private_dir(Path(self.tmp.name)/'private')
        self.script=self.root/'worker.py'


class PublicProviderBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.home=core.private_dir(Path(self.tmp.name)/'home')

    def test_no_interpreter_helper_lookup_or_child(self):
        runner=WorkspaceRunner(self.home,request=lambda *a,**k:SimpleNamespace(status=200,body=b'{}',headers={},json=lambda:{}))
        with patch('subprocess.Popen',side_effect=AssertionError('no child')),patch('sys.executable','untrusted-relative-python'):
            self.assertEqual(runner('metadata','file'),{})
            self.assertEqual(WorkspaceDrive(self.home,'a'*64,self.home/'state').status()['code'],'GOOGLE_AUTH_SETUP_REQUIRED')

    def test_redirect_status_failures_and_large_metadata_are_closed(self):
        for status,body in [(302,b'PRIVATE_LOCATION'),(403,b'PRIVATE_PROVIDER'),(500,b'PRIVATE_ERROR'),(200,b'x'*(1024*1024+1))]:
            with self.subTest(status=status):
                response=SimpleNamespace(status=status,body=body,headers={})
                runner=WorkspaceRunner(self.home,request=lambda *a,**k:response)
                runner.drive.sleep=lambda _:None
                with self.assertRaises(core.FinixError) as failure:
                    runner('metadata','file')
                self.assertNotIn('PRIVATE',str(failure.exception))

    def test_timeout_is_explicit_exception_is_private(self):
        calls=[]
        def request(*a,**kw):
            calls.append(kw)
            raise TimeoutError('PRIVATE_PATH_TOKEN_SENTINEL')
        with self.assertRaises(core.FinixError) as failure:
            WorkspaceRunner(self.home,request=request)('identity')
        self.assertEqual(calls[0]['timeout'],20)
        self.assertNotIn('SENTINEL',str(failure.exception))

    def test_cancel_before_and_after_request(self):
        for after in (False,True):
            called=[]
            def request(*a,**kw):
                called.append(True)
                return SimpleNamespace(status=200,body=b'{}',headers={},json=lambda:{})
            with self.assertRaisesRegex(core.FinixError,'cancelled'):
                WorkspaceRunner(self.home,request=request)('metadata','file',cancel=lambda:not after or bool(called))
            self.assertEqual(bool(called),after)

    def test_range_overflow_short_wrong_range_encoding_and_redirect(self):
        for status,headers,body in [
            (206,{'Content-Range':'bytes 0-3/4'},b'12345'),
            (206,{'Content-Range':'bytes 0-3/4'},b'123'),
            (206,{'Content-Range':'bytes 1-4/4'},b'1234'),
            (206,{'Content-Range':'bytes 0-3/4','Content-Encoding':'gzip'},b'1234'),
            (302,{},b'1234'),(200,{},b'1234')]:
            response=SimpleNamespace(status=status,headers=headers,body=body)
            runner=WorkspaceRunner(self.home,request=lambda *a,**k:response)
            output=Path(self.tmp.name)/'cipher.age'
            with self.assertRaises(core.FinixError):
                runner('download','file','--output',str(output),expected_size=4)
            self.assertEqual(output.read_bytes(),b'')
            output.unlink()

    def test_exclusive_sink_exact_bytes_explicit_range(self):
        calls=[]
        def request(*a,**kw):
            calls.append(kw)
            return SimpleNamespace(status=206,body=b'1234',headers={'Content-Range':'bytes 0-3/4'})
        runner=WorkspaceRunner(self.home,request=request)
        output=Path(self.tmp.name)/'cipher.age'
        self.assertEqual(runner('download','file','--output',str(output),expected_size=4)['status'],'downloaded')
        self.assertEqual(output.read_bytes(),b'1234')
        self.assertEqual(calls[0]['headers'],{'Range':'bytes=0-3','Accept-Encoding':'identity'})
        self.assertEqual(calls[0]['timeout'],120)
        with self.assertRaises(FileExistsError):
            runner('download','file','--output',str(output),expected_size=4)

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



if __name__=='__main__':
    unittest.main()
