"""Synthetic only: no live Google or real profile auth is opened."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from finix import core
from finix.integration import Profile
from finix.workspace_drive import WorkspaceDrive, WorkspaceRunner, WorkspaceError, MIME, FOLDER_MIME, child_env, helper_path


class FakeDrive:
    def __init__(self):
        self.objects, self.data, self.calls = {}, {}, []
        self.corrupt, self.bad_parent, self.fail_delete = False, False, None

    def __call__(self, op, *args, **kwargs):
        self.calls.append((op, args))
        if op == 'metadata':
            if args[0] not in self.objects:
                raise WorkspaceError('NOT_FOUND')
            return dict(self.objects[args[0]])
        if op == 'create-folder':
            obj = 'folder-' + str(len(self.objects))
            self.objects[obj] = dict(id=obj, mimeType=FOLDER_MIME, owners=[{'emailAddress': 'synthetic@example.invalid'}], trashed=False)
            return {'id': obj, 'status': 'created'}
        if op == 'upload':
            obj = 'file-' + uuid.uuid4().hex
            data = Path(args[0]).read_bytes()
            folder = args[args.index('--parent') + 1]
            self.data[obj] = data
            self.objects[obj] = dict(id=obj, mimeType=MIME, size=str(len(data)), parents=['foreign' if self.bad_parent else folder],
                                     owners=[{'emailAddress': 'synthetic@example.invalid'}], trashed=False)
            return {'id': obj, 'status': 'uploaded', 'mimeType': MIME}
        if op == 'download':
            Path(args[-1]).write_bytes(b'corrupt' if self.corrupt else self.data[args[0]])
            return {'id': args[0], 'status': 'downloaded'}
        if op == 'delete':
            if args[0] == self.fail_delete:
                raise WorkspaceError('PROVIDER_ERROR')
            self.objects[args[0]]['trashed'] = True
            return {'fileId': args[0], 'status': 'trashed', 'permanent': False}
        raise AssertionError(op)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'profile'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.fake = FakeDrive()
        self.drive = WorkspaceDrive(self.home, 'a' * 64, self.state, runner=self.fake)
        self.path = self.root / 'input.age'
        self.path.write_bytes(b'age-encryption.org/v1\nsynthetic-ciphertext')

    def completion(self):
        # Synthetic transport fixtures explicitly attest their known provenance.
        return dict(sha256=core.digest_file(self.path), complete=True, omitted_count=0)

    def test_upload_recovery_restart_retention_and_foreign(self):
        foreign = dict(id='foreign', mimeType=MIME, parents=['irrelevant'], trashed=False)
        self.fake.objects['foreign'] = foreign
        first = self.drive.backup_cipher(self.path, completion=self.completion())
        record = self.drive.backup_cipher(self.path, completion=self.completion())
        restarted = WorkspaceDrive(self.home, 'a' * 64, self.state, runner=self.fake)
        latest = restarted.backup_cipher(self.path, completion=self.completion(), keep=1)
        self.assertTrue(self.fake.objects[first['id']]['trashed'])
        self.assertTrue(self.fake.objects[record['id']]['trashed'])
        self.assertFalse(self.fake.objects['foreign']['trashed'])
        output = self.root / 'recovered.age'
        # Independent recovery doesn't consult original ledger or create state.
        recovery = WorkspaceDrive(self.home, 'a' * 64, self.root / 'absent', runner=self.fake)
        recovery.download(latest, output)
        self.assertEqual(output.read_bytes(), self.path.read_bytes())
        self.assertFalse((self.root / 'absent').exists())
        with self.assertRaises(FileExistsError):
            recovery.download(latest, output)

    def test_corrupt_and_wrong_parent_do_not_index_or_prune(self):
        first = self.drive.backup_cipher(self.path, completion=self.completion())
        for flag in ('corrupt', 'bad_parent'):
            setattr(self.fake, flag, True)
            with self.assertRaises(core.FinixError):
                self.drive.backup_cipher(self.path, completion=self.completion(), keep=1)
            setattr(self.fake, flag, False)
        self.assertFalse(self.fake.objects[first['id']]['trashed'])
        self.assertEqual(len(json.loads((self.state / 'workspace.json').read_text())['records']), 1)
        self.assertTrue(self.path.exists())

    def test_missing_ledger_new_namespace_and_binding(self):
        first = self.drive.backup_cipher(self.path, completion=self.completion())
        (self.state / 'workspace.json').unlink()
        new = self.drive.backup_cipher(self.path, completion=self.completion(), keep=1)
        self.assertNotEqual(first['folder_id'], new['folder_id'])
        self.assertFalse(self.fake.objects[first['id']]['trashed'])
        wrong = WorkspaceDrive(self.home, 'b' * 64, self.state, runner=self.fake)
        with self.assertRaises(core.FinixError):
            wrong.backup_cipher(self.path, completion=self.completion(), keep=1)

    def test_partial_prune_restart_404_and_owner_changed(self):
        records = [self.drive.backup_cipher(self.path, completion=self.completion()) for _ in range(3)]
        self.fake.fail_delete = records[1]['id']
        with self.assertRaises(WorkspaceError):
            self.drive.backup_cipher(self.path, completion=self.completion(), keep=1)
        journal = json.loads((self.state / 'workspace.json').read_text())
        self.assertNotIn(records[0]['id'], [x['id'] for x in journal['records']])
        del self.fake.objects[records[1]['id']]
        self.fake.fail_delete = None
        self.drive.backup_cipher(self.path, completion=self.completion(), keep=1)
        self.assertEqual(len(json.loads((self.state / 'workspace.json').read_text())['records']), 1)
        row = self.drive.backup_cipher(self.path, completion=self.completion())
        self.fake.objects[row['id']]['owners'] = [{'emailAddress': 'changed@example.invalid'}]
        with self.assertRaises(core.FinixError):
            self.drive.backup_cipher(self.path, completion=self.completion(), keep=1)
        self.assertFalse(self.fake.objects[row['id']]['trashed'])

    def test_profile_handler_e2e_age_restore_and_failure_preserves_local(self):
        if not shutil.which('age'):
            self.skipTest('age required')
        identity = self.root / 'recovery.key'
        recipient = core.keygen(identity)
        (self.home / 'config.yaml').write_text('synthetic: true\n')
        profile = Profile(self.home)
        profile.recipient(profile.profile_id, recipient, None)
        # Local readiness must not execute the runner.
        status = profile.configure('storage', 'workspace')
        self.assertFalse(status['workspace']['live_google_verified'])
        adapter = WorkspaceDrive(self.home, profile.profile_id, profile.root / 'workspace', runner=self.fake)
        with patch.object(Profile, 'workspace', return_value=adapter):
            result = profile.backup()
            record = result['workspace']
            self.assertTrue(Path(result['path'] + '.workspace.json').is_file())
            downloaded = self.root / 'download.age'
            adapter.download(record, downloaded)
            target = self.root / 'restored'
            core.restore(downloaded, identity, target)
            self.assertEqual((target / 'config.yaml').read_bytes(), (self.home / 'config.yaml').read_bytes())
            self.fake.corrupt = True
            with self.assertRaises(core.FinixError):
                profile.backup()
            self.assertGreaterEqual(len(list((profile.root / 'copies').glob('*.age'))), 2)

    def test_readiness_presence_only_missing_deps_and_minimal_env(self):
        with patch('finix.workspace_drive.helper_path', return_value=self.path), patch('finix.workspace_drive.WorkspaceRunner.__call__', return_value={'dependencies_ready': True}):
            self.assertEqual(self.drive.status()['code'], 'TOKEN_ABSENT')
            (self.home / 'google_token.json').write_text('invalid synthetic token, must not parse')
            self.assertEqual(self.drive.status()['code'], 'LOCAL_READY')
        with patch('finix.workspace_drive.helper_path', return_value=self.path), patch('finix.workspace_drive.WorkspaceRunner.__call__', return_value={'dependencies_ready': False}):
            self.assertEqual(self.drive.status()['code'], 'DEPENDENCIES_MISSING')
        with patch.dict(os.environ, {'HERMES_HOME': '/wrong', 'GOOGLE_APPLICATION_CREDENTIALS': '/secret', 'HERMES_GWS_BIN': '/hostile', 'PYTHONPATH': '/hostile'}):
            env = child_env(self.home)
        self.assertEqual(env['HERMES_HOME'], str(self.home))
        self.assertFalse(set(env) & {'GOOGLE_APPLICATION_CREDENTIALS', 'HERMES_GWS_BIN', 'PYTHONPATH'})

    def test_subprocess_real_helper_profile_timeout_output_and_filesize(self):
        script = self.root / 'synthetic_api.py'
        script.write_text('import os,json\ndef main():\n print(json.dumps({"home": os.environ["HERMES_HOME"]}))\n')
        runner = WorkspaceRunner(self.home, script=script, timeout=2)
        self.assertEqual(runner('probe')['home'], str(self.home))
        script.write_text('import time\ndef main():\n time.sleep(10)\n')
        runner.timeout = .2
        start = time.monotonic()
        with self.assertRaisesRegex(WorkspaceError, 'TIMEOUT'):
            runner('probe')
        self.assertLess(time.monotonic() - start, 3)
        runner.timeout = 2
        script.write_text('def main():\n print("secret" * 20000)\n')
        with self.assertRaisesRegex(WorkspaceError, 'OUTPUT_LIMIT'):
            runner('probe')
        file = self.root / 'bounded'
        script.write_text('def main():\n open(' + repr(str(file)) + ',"wb").write(b"x"*8192)\n')
        runner.max_bytes = 1024
        with self.assertRaises(WorkspaceError):
            runner('probe')
        self.assertLessEqual(file.stat().st_size, 1024)

    def test_cli_recovery_and_cipher_bounds(self):
        from finix.workspace_drive import main
        record = self.drive.backup_cipher(self.path, completion=self.completion())
        saved = self.root / 'public.json'
        saved.write_text(json.dumps(record))
        output = self.root / 'cli.age'
        argv = ['download', '--home', str(self.home), '--profile-id', self.drive.owner,
                '--record', str(saved), '--output', str(output)]
        with patch('finix.workspace_drive.WorkspaceDrive', return_value=self.drive), redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)
        self.assertEqual(output.read_bytes(), self.path.read_bytes())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.drive.backup_cipher(self.path, completion=self.completion())['verified'])
        link = self.root / 'link.age'
        link.symlink_to(self.path)
        with self.assertRaises(core.FinixError):
            self.drive.backup_cipher(link)

    def test_actual_installed_wrapper_contract_without_auth(self):
        # Actual public installed source, fake service only; no get_credentials.
        selected = os.environ.get('FINIX_WORKSPACE_SOURCE')
        script = Path(selected) if selected else helper_path(self.home)
        if script is None or not script.is_file():
            self.skipTest('installed public helper unavailable; set FINIX_WORKSPACE_SOURCE to its script path')
        import sys
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}), patch.object(sys, 'path', [str(script.parent), *sys.path]):
            spec = importlib.util.spec_from_file_location('finix_contract_only', script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        class Request:
            def execute(self):
                return {'id': 'synthetic-id', 'name': 'cipher.age', 'mimeType': MIME}
        class Service:
            def files(self):
                return self
            def create(self, **kwargs):
                self.kwargs = kwargs
                return Request()
            def get(self, **kwargs):
                self.kwargs = kwargs
                return Request()
            def get_media(self, **kwargs):
                self.kwargs = kwargs
                return b'synthetic binary'
            def update(self, **kwargs):
                self.kwargs = kwargs
                return Request()
        service = Service()
        with patch.object(module, 'build_service', return_value=service), redirect_stdout(io.StringIO()) as out:
            module.drive_upload(SimpleNamespace(path=str(self.path), name='cipher.age', parent='folder', mime_type=MIME))
        self.assertEqual(json.loads(out.getvalue())['status'], 'uploaded')
        self.assertEqual(service.kwargs['body']['parents'], ['folder'])
        self.assertTrue(service.kwargs['media_body'].resumable())
        self.assertEqual(module.TOKEN_PATH, self.home / 'google_token.json')
        class Downloader:
            def __init__(self, stream, request):
                self.stream, self.request = stream, request
            def next_chunk(self):
                self.stream.write(self.request)
                return None, True
        output = self.root / 'contract-download.age'
        with patch.object(module, 'build_service', return_value=service), patch.object(module, '_gws_binary', return_value=None), patch('googleapiclient.http.MediaIoBaseDownload', Downloader), redirect_stdout(io.StringIO()) as out:
            module.drive_download(SimpleNamespace(file_id='synthetic-id', output=str(output), export_mime=None))
        self.assertEqual(json.loads(out.getvalue())['status'], 'downloaded')
        self.assertEqual(output.read_bytes(), b'synthetic binary')
        with patch.object(module, 'build_service', return_value=service), patch.object(module, '_gws_binary', return_value=None), redirect_stdout(io.StringIO()) as out:
            module.drive_delete(SimpleNamespace(file_id='synthetic-id', permanent=False))
        self.assertEqual(json.loads(out.getvalue())['status'], 'trashed')
        self.assertEqual(service.kwargs, {'fileId': 'synthetic-id', 'body': {'trashed': True}})


if __name__ == '__main__':
    unittest.main()
