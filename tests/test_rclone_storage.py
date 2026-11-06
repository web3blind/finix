"""No cloud accounts: synthetic contracts and opt-in real rclone local transport."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace

from finix import core
from finix.integration import Profile, command
from finix.rclone_storage import main
from finix.rclone_storage import RcloneStorage, RcloneRunner, RcloneError, remote_name, relative_root, child_env


class MemoryRunner:
    def __init__(self):
        self.objects = {}
        self.corrupt = False
        self.fail_delete = None
        self.calls = []

    def __call__(self, op, *args, output=None, expected_size=None):
        self.calls.append((op, args))
        if op == 'listremotes':
            return json.dumps([{'name': 'cloud', 'type': 'yandex', 'source': 'file'}]).encode()
        if op == 'copyto':
            self.objects[args[1]] = Path(args[0]).read_bytes()
            return b''
        if op == 'lsjson':
            if args[0] not in self.objects:
                raise RcloneError('NOT_FOUND')
            return json.dumps({'Name': args[0].split('/')[-1], 'Size': len(self.objects[args[0]]), 'IsDir': False}).encode()
        if op == 'cat':
            if args[0] not in self.objects:
                raise RcloneError('NOT_FOUND')
            data = b'corrupt' if self.corrupt else self.objects[args[0]]
            output.write(data)
            return b''
        if op == 'deletefile':
            if args[0] == self.fail_delete:
                raise RcloneError('PROVIDER_ERROR')
            self.objects.pop(args[0], None)
            return b''
        raise AssertionError(op)


class RcloneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.confdir = self.root / 'auth'
        self.confdir.mkdir(mode=0o700)
        self.config = self.confdir / 'rclone.conf'
        self.config.write_text('[cloud]\ntype = yandex\n')
        self.config.chmod(0o600)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.fake = MemoryRunner()
        self.drive = self.adapter()
        self.cipher = self.root / 'input.age'
        self.cipher.write_bytes(b'age-encryption.org/v1\nsynthetic-only')

    def adapter(self, owner='a' * 64, state=None, runner=None):
        return RcloneStorage(self.home, owner, state or self.state, str(self.config), 'cloud', 'backups/Finix', runner=runner or self.fake)

    def test_roundtrip_retention_recovery_and_ledger_loss(self):
        foreign = 'cloud:backups/Finix/foreign.age'
        self.fake.objects[foreign] = b'foreign'
        first = self.drive.backup_cipher(self.cipher)
        last = self.adapter().backup_cipher(self.cipher, keep=1)
        self.assertNotIn(self.drive.object_path(first), self.fake.objects)
        self.assertIn(foreign, self.fake.objects)
        output = self.root / 'recovered.age'
        self.adapter(state=self.root / 'absent').download(last, output)
        self.assertEqual(output.read_bytes(), self.cipher.read_bytes())
        self.assertFalse((self.root / 'absent').exists())
        with self.assertRaises(FileExistsError):
            self.drive.download(last, output)
        (self.state / 'rclone.json').unlink()
        new = self.drive.backup_cipher(self.cipher, keep=1)
        self.assertNotEqual(new['namespace'], last['namespace'])
        self.assertIn(self.drive.object_path(last), self.fake.objects)

    def test_corruption_plaintext_foreign_profile_config_and_no_prune(self):
        first = self.drive.backup_cipher(self.cipher)
        self.fake.corrupt = True
        with self.assertRaises(core.FinixError):
            self.drive.backup_cipher(self.cipher, keep=1)
        self.assertIn(self.drive.object_path(first), self.fake.objects)
        self.assertEqual(len(json.loads((self.state / 'rclone.json').read_text())['records']), 1)
        self.fake.corrupt = False
        with self.assertRaises(core.FinixError):
            self.adapter(owner='b' * 64).backup_cipher(self.cipher)
        calls = len(self.fake.calls)
        self.cipher.write_bytes(b'plaintext')
        with self.assertRaises(core.FinixError):
            self.drive.backup_cipher(self.cipher)
        self.assertEqual(len(self.fake.calls), calls)
        self.config.chmod(0o644)
        with self.assertRaises(core.FinixError):
            self.drive.download(first)

    def test_interrupted_retention_reconciles_already_missing(self):
        rows = [self.drive.backup_cipher(self.cipher) for _ in range(3)]
        self.fake.fail_delete = self.drive.object_path(rows[1])
        with self.assertRaises(RcloneError):
            self.drive.backup_cipher(self.cipher, keep=1)
        self.assertNotIn(self.drive.object_path(rows[0]), self.fake.objects)
        self.fake.objects.pop(self.drive.object_path(rows[1]))
        self.fake.fail_delete = None
        self.drive.backup_cipher(self.cipher, keep=1)
        self.assertEqual(len(json.loads((self.state / 'rclone.json').read_text())['records']), 1)

    def test_validation_no_inline_remote_flags_traversal_or_symlink(self):
        for value in (':local:', 'cloud:', '-flag', 'cloud,x=y', 'cloud/path', 'cloud?x', ''):
            with self.assertRaises(core.FinixError):
                remote_name(value)
        for value in ('../x', '/absolute', 'x//y', './x', 'x/../y', 'x:y', 'x\\y', ''):
            with self.assertRaises(core.FinixError):
                relative_root(value)
        linked = self.confdir / 'linked'
        linked.symlink_to(self.config)
        with self.assertRaises(core.FinixError):
            RcloneStorage(self.home, 'a' * 64, self.state, str(linked), 'cloud', 'root', runner=self.fake)
        self.confdir.chmod(0o755)
        self.assertEqual(self.drive.status()['code'], 'CONFIG_PERMISSIONS')
        self.confdir.chmod(0o700)
        self.config.unlink()
        self.assertEqual(self.drive.status()['code'], 'CONFIG_UNAVAILABLE')

    def test_local_readiness_unknown_type_and_minimal_env(self):
        self.assertEqual(self.drive.status()['code'], 'LOCAL_READY')
        self.assertTrue(all(op == 'listremotes' for op, _ in self.fake.calls))
        self.drive.remote = 'unknown'
        self.assertEqual(self.drive.status()['code'], 'REMOTE_UNAVAILABLE')
        with patch.dict(os.environ, {'RCLONE_CONFIG_PASS': 'secret', 'RCLONE_CONFIG_CLOUD_TOKEN': 'secret', 'AWS_SECRET_ACCESS_KEY': 'secret', 'PYTHONPATH': '/wrong'}):
            env = child_env(self.home)
        self.assertFalse(any(k.startswith(('RCLONE_', 'AWS_')) for k in env))
        self.assertNotIn('PYTHONPATH', env)
        self.assertEqual(env['HERMES_HOME'], str(self.home))

    def test_runner_missing_timeout_output_and_noninteractive_encrypted(self):
        runner = RcloneRunner(self.home, self.config, binary=str(self.root / 'missing'))
        with self.assertRaisesRegex(RcloneError, 'BINARY_MISSING'):
            runner('listremotes', '--json')
        binary = self.root / 'fake-rclone'
        binary.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(20)\n')
        binary.chmod(0o700)
        runner = RcloneRunner(self.home, self.config, binary=str(binary), timeout=.2)
        start = time.monotonic()
        with self.assertRaisesRegex(RcloneError, 'TIMEOUT'):
            runner('listremotes', '--json')
        self.assertLess(time.monotonic() - start, 3)
        binary.write_text('#!/usr/bin/python3\nprint("secret" * 20000)\n')
        runner.timeout = 2
        with self.assertRaisesRegex(RcloneError, 'OUTPUT_LIMIT'):
            runner('listremotes', '--json')
        binary.write_text('#!/usr/bin/python3\nimport sys\nassert "--ask-password=false" in sys.argv\nassert sys.stdin.read() == ""\nsys.exit(1)\n')
        with self.assertRaisesRegex(RcloneError, 'PROVIDER_ERROR'):
            runner('listremotes', '--json')

    def test_profile_configuration_and_public_descriptor(self):
        profile = Profile(self.home)
        for key, value in [('rclone_config', str(self.config)), ('rclone_remote', 'cloud'), ('rclone_root', 'backups/Finix')]:
            profile.configure(key, value)
        with patch.object(Profile, 'rclone', return_value=self.drive):
            self.assertEqual(profile.configure('storage', 'rclone')['rclone']['code'], 'LOCAL_READY')
        self.assertEqual(profile.config()['storage'], 'rclone')
        row = self.drive.backup_cipher(self.cipher)
        serialized = json.dumps(row)
        self.assertNotIn(str(self.config), serialized)
        self.assertNotIn('token', serialized)

    def test_recovery_cli_exclusive_output_and_exact_handler_owner(self):
        row = self.drive.backup_cipher(self.cipher)
        record = self.root / 'record.json'
        record.write_text(json.dumps(row))
        output = self.root / 'cli.age'
        args = ['download', '--home', str(self.home), '--profile-id', 'a' * 64,
                '--config', str(self.config), '--remote', 'cloud', '--root', 'backups/Finix',
                '--record', str(record), '--output', str(output)]
        with patch('finix.rclone_storage.RcloneStorage', return_value=self.drive), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 1)
        self.assertEqual(output.read_bytes(), self.cipher.read_bytes())
        profile = Profile(self.home)
        with patch('finix.integration.current_profile', return_value=profile):
            result = json.loads(command('configure storage rclone', owner_id='42', owner_platform='telegram',
                                        platform='telegram', source=SimpleNamespace(user_id='42')))
            self.assertEqual(result['rclone']['code'], 'SELECTION_REQUIRED')
            self.assertIn('denied', command('configure storage local'))
        # Reject auth files inside any backed-up profile tree.
        forbidden = self.home / 'secrets'
        forbidden.mkdir(mode=0o700)
        config = forbidden / 'rclone.conf'
        config.write_text('[cloud]\ntype=yandex\n')
        config.chmod(0o600)
        with self.assertRaises(core.FinixError):
            profile.configure('rclone_config', str(config))

    def test_changed_selection_metadata_and_non_age_input_fail_closed(self):
        row = self.drive.backup_cipher(self.cipher)
        self.drive.root = 'different'
        with self.assertRaises(core.FinixError):
            self.drive.backup_cipher(self.cipher, keep=1)
        self.drive.root = 'backups/Finix'
        self.fake.objects[self.drive.object_path(row)] += b'changed'
        with self.assertRaises(core.FinixError):
            self.drive.download(row)
        self.assertIn(self.drive.object_path(row), self.fake.objects)
        with self.cipher.open('wb') as stream:
            stream.truncate(129 * 1024 * 1024 + 1)
        with self.assertRaises(core.FinixError):
            self.drive.backup_cipher(self.cipher)

    def test_all_admitted_types_and_cloud_failure_preserves_local(self):
        original = self.fake
        for kind in ('yandex', 'onedrive', 'dropbox'):
            def transport(op, *args, **kwargs):
                if op == 'listremotes':
                    return json.dumps([{'name': 'cloud', 'type': kind, 'source': 'file'}]).encode()
                return original(op, *args, **kwargs)
            adapter = self.adapter(state=self.root / kind, runner=transport)
            row = adapter.backup_cipher(self.cipher)
            self.assertEqual(row['type'], kind)
            self.assertTrue(adapter.download(row)['verified'])
        profile = Profile(self.home)
        identity = self.root / 'test-recovery.key'
        recipient = core.keygen(identity)
        (self.home / 'config.yaml').write_text('synthetic: true\n')
        profile.recipient(profile.profile_id, recipient, None)
        adapter = self.adapter(owner=profile.profile_id, state=self.root / 'profile-state')
        with patch.object(Profile, 'rclone', return_value=adapter):
            profile.configure('storage', 'rclone')
            profile.configure('retention', 1)
            first = profile.backup()
            self.fake.corrupt = True
            with self.assertRaises(core.FinixError):
                profile.backup()
        self.assertIsInstance(first['path'], str)
        self.assertTrue(Path(str(first['path'])).is_file())
        self.assertEqual(len(list((profile.root / 'copies').glob('*.age'))), 2)

    @unittest.skipUnless(os.environ.get('FINIX_RCLONE_BINARY'), 'explicit reviewed rclone binary required')
    def test_real_encrypted_config_no_prompt_or_secret_diagnostics(self):
        self.config.write_text('RCLONE_ENCRYPT_V0:\ninvalid-synthetic-encrypted-config\n')
        runner = RcloneRunner(self.home, self.config, binary=os.environ['FINIX_RCLONE_BINARY'], timeout=2)
        start = time.monotonic()
        with self.assertRaises(RcloneError) as caught:
            runner('listremotes', '--json')
        self.assertLess(time.monotonic() - start, 3)
        self.assertNotIn('invalid-synthetic', str(caught.exception))

    @unittest.skipUnless(os.environ.get('FINIX_RCLONE_BINARY'), 'set explicit reviewed rclone binary for local-only smoke')
    def test_real_local_rclone_age_backup_download_restore(self):
        # Only the injected contract maps local -> yandex. Production rejects local.
        local = self.root / 'cloud'
        local.mkdir()
        self.config.write_text('[cloud]\ntype = local\n')
        real = RcloneRunner(self.home, self.config, binary=os.environ['FINIX_RCLONE_BINARY'])
        def transport(op, *args, **kwargs):
            if op == 'listremotes':
                actual = json.loads(real(op, *args))
                self.assertEqual(actual[0]['type'], 'local')
                return json.dumps([dict(actual[0], type='yandex')]).encode()
            # Absolute local root is confined to this disposable directory.
            args = tuple(a.replace('cloud:backups/Finix', 'cloud:' + str(local)) if isinstance(a, str) else a for a in args)
            return real(op, *args, **kwargs)
        identity = self.root / 'identity.key'
        recipient = core.keygen(identity)
        (self.home / 'config.yaml').write_text('synthetic: local-only\n')
        profile = Profile(self.home)
        profile.recipient(profile.profile_id, recipient, None)
        self.drive = self.adapter(owner=profile.profile_id, runner=transport)
        with patch.object(Profile, 'rclone', return_value=self.drive):
            profile.configure('storage', 'rclone')
            profile.configure('retention', 1)
            profile.backup()
            result = profile.backup()
        output = self.root / 'download.age'
        self.drive.download(result['rclone'], output)
        target = self.root / 'restored'
        core.restore(output, identity, target)
        self.assertEqual((target / 'config.yaml').read_bytes(), (self.home / 'config.yaml').read_bytes())
        self.assertEqual(len(list(local.glob('*/*.age'))), 1)
        self.assertTrue(Path(result['path'] + '.rclone.json').is_file())
        # Exercise standalone recovery CLI (with local type deliberately rejected).
        self.assertFalse(RcloneStorage(self.home, profile.profile_id, self.root / 'unused', str(self.config), 'cloud', 'backups/Finix', runner=real).status()['ready'])


if __name__ == '__main__':
    unittest.main()
