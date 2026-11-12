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
from finix.workspace_drive import WorkspaceDrive, WorkspaceRunner, WorkspaceError, MIME, FOLDER_MIME, child_env


from test_workspace_reconcile import Transport, ACCOUNT_DIGEST

class FakeDrive(Transport):
    def __init__(self):
        super().__init__()
        self.corrupt, self.bad_parent, self.fail_delete = False, False, None

    def __call__(self, op, *args, **kwargs):
        if op == 'delete':
            if args[0] == self.fail_delete:
                raise WorkspaceError('PROVIDER_ERROR')
            self.objects[args[0]]['trashed'] = True
            return {'fileId': args[0], 'status': 'trashed', 'permanent': False}
        result = super().__call__(op, *args, **kwargs)
        if op == 'upload' and self.bad_parent:
            self.objects[result['id']]['parents'] = ['foreign']
        if op == 'download' and self.corrupt:
            Path(args[args.index('--output')+1]).write_bytes(b'corrupt')
        return result


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = core.private_dir(Path(self.tmp.name) / 'private')
        self.home = core.private_dir(self.root / 'profile')
        self.state = self.root / 'state'
        self.fake = FakeDrive()
        self.drive = WorkspaceDrive(self.home, 'a' * 64, self.state, runner=self.fake, account_digest=ACCOUNT_DIGEST)
        self.path = self.root / 'input.age'
        self.path.write_bytes(b'age-encryption.org/v1\nsynthetic-ciphertext')

    def backup(self, adapter=None, keep=0):
        self.path.write_bytes(self.path.read_bytes() + b'next')
        return (adapter or self.drive).backup_cipher(self.path, keep=keep, completion=self.completion())

    def completion(self):
        # Synthetic transport fixtures explicitly attest their known provenance.
        return dict(sha256=core.digest_file(self.path), complete=True, omitted_count=0)

    def test_upload_recovery_restart_retention_and_foreign(self):
        foreign = dict(id='foreign', mimeType=MIME, parents=['irrelevant'], trashed=False)
        self.fake.objects['foreign'] = foreign
        first = self.backup()
        record = self.backup()
        restarted = WorkspaceDrive(self.home, 'a' * 64, self.state, runner=self.fake, account_digest=ACCOUNT_DIGEST)
        latest = self.backup(restarted, keep=1)
        self.assertTrue(self.fake.objects[first['id']]['trashed'])
        self.assertTrue(self.fake.objects[record['id']]['trashed'])
        self.assertFalse(self.fake.objects['foreign']['trashed'])
        output = self.root / 'recovered.age'
        # Independent recovery doesn't consult original ledger or create state.
        recovery = WorkspaceDrive(self.home, 'a' * 64, self.root / 'absent', runner=self.fake, account_digest=ACCOUNT_DIGEST)
        recovery.download(latest, output)
        self.assertEqual(output.read_bytes(), self.path.read_bytes())
        self.assertFalse((self.root / 'absent').exists())
        with self.assertRaises(FileExistsError):
            recovery.download(latest, output)

    def test_corrupt_and_wrong_parent_do_not_index_or_prune(self):
        first = self.backup()
        for flag in ('corrupt', 'bad_parent'):
            setattr(self.fake, flag, True)
            with self.assertRaises(core.FinixError):
                self.backup(keep=1)
            setattr(self.fake, flag, False)
        self.assertFalse(self.fake.objects[first['id']]['trashed'])
        self.assertEqual(len(json.loads((self.state / 'workspace.json').read_text())['records']), 1)
        self.assertTrue(self.path.exists())

    def test_missing_ledger_new_namespace_and_binding(self):
        first = self.backup()
        (self.state / 'workspace.json').unlink()
        new = self.backup(keep=1)
        self.assertNotEqual(first['folder_id'], new['folder_id'])
        self.assertFalse(self.fake.objects[first['id']]['trashed'])
        wrong = WorkspaceDrive(self.home, 'b' * 64, self.state, runner=self.fake, account_digest=ACCOUNT_DIGEST)
        with self.assertRaises(core.FinixError):
            wrong.backup_cipher(self.path, completion=self.completion(), keep=1)

    def test_partial_prune_restart_404_and_owner_changed(self):
        records = [self.backup() for _ in range(3)]
        self.fake.fail_delete = records[1]['id']
        with self.assertRaises(WorkspaceError):
            self.backup(keep=1)
        journal = json.loads((self.state / 'workspace.json').read_text())
        self.assertNotIn(records[0]['id'], [x['id'] for x in journal['records']])
        del self.fake.objects[records[1]['id']]
        self.fake.fail_delete = None
        self.backup(keep=1)
        self.assertEqual(len(json.loads((self.state / 'workspace.json').read_text())['records']), 1)
        row = self.backup()
        self.fake.objects[row['id']]['owners'] = [{'emailAddress': 'changed@example.invalid'}]
        with self.assertRaises(core.FinixError):
            self.backup(keep=1)
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
        adapter = WorkspaceDrive(self.home, profile.profile_id, profile.root / 'workspace', runner=self.fake, account_digest=ACCOUNT_DIGEST)
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

    def test_readiness_does_not_load_credentials_or_launch_helpers(self):
        with patch('finix.workspace_drive.WorkspaceRunner.__call__', side_effect=AssertionError('no probe')):
            self.assertEqual(self.drive.status()['code'], 'GOOGLE_AUTH_SETUP_REQUIRED')
        with patch.dict(os.environ, {'HERMES_HOME': '/wrong', 'GOOGLE_APPLICATION_CREDENTIALS': '/secret', 'HERMES_GWS_BIN': '/hostile', 'PYTHONPATH': '/hostile'}):
            env = child_env(self.home)
        self.assertEqual(env['HERMES_HOME'], str(self.home))
        self.assertFalse(set(env) & {'GOOGLE_APPLICATION_CREDENTIALS', 'HERMES_GWS_BIN', 'PYTHONPATH'})

    def test_removed_helper_options_cannot_execute_arbitrary_script(self):
        script = self.root / 'synthetic_api.py'
        script.write_text('raise AssertionError("must not import")')
        runner = WorkspaceRunner(self.home, script=script, timeout=2)
        with self.assertRaisesRegex(WorkspaceError, 'UNSUPPORTED_OPERATION'):
            runner('probe')

    def test_cli_recovery_and_cipher_bounds(self):
        from finix.workspace_drive import main
        record = self.backup()
        saved = self.root / 'public.json'
        saved.write_text(json.dumps(record))
        output = self.root / 'cli.age'
        argv = ['download', '--home', str(self.home), '--profile-id', self.drive.owner,
                '--record', str(saved), '--output', str(output)]
        with patch('finix.workspace_drive.WorkspaceDrive', return_value=self.drive), redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)
        self.assertEqual(output.read_bytes(), self.path.read_bytes())
        if os.name != 'nt':
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        else:
            from finix.windows_native import regular, trusted_handle
            import msvcrt
            with regular(output) as fd:
                self.assertTrue(trusted_handle(msvcrt.get_osfhandle(fd), private=True))
        self.assertTrue(self.backup()['verified'])
        if os.name != 'nt':
            link = self.root / 'link.age'
            link.symlink_to(self.path)
            with self.assertRaises(core.FinixError):
                self.drive.backup_cipher(link)

    def test_declared_public_contract_replaces_skill_wrapper(self):
        from test_reviewer_provider_contract import ProviderContract
        ProviderContract('test_public_transport_identity_id_and_metadata').test_public_transport_identity_id_and_metadata()


if __name__ == '__main__':
    unittest.main()
