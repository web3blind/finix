"""Local-only reviewer security regressions; no providers or authorization."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from finix import core, streaming


def archive_for(names):
    data = b'x'
    manifest = {'format': 'finix-2', 'files': {n: {'size': 1, 'sha256': hashlib.sha256(data).hexdigest()} for n in names}}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for name, payload in [('profile/' + n, data) for n in names] + [('manifest.json', json.dumps(manifest).encode())]:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    archive.seek(0)
    return archive


class CasePolicyTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX synthetic fixture')
    def test_synthetic_oauth_fixture_blocks_real_binary_fallback(self):
        from test_connections import ConnectionsTests
        test = ConnectionsTests('runTest')
        try:
            test.setUp()
            with patch.dict(os.environ, PATH='/nonexistent-finix-fixture'):
                import shutil
                with self.assertRaises(AssertionError):
                    shutil.which('rclone')
        finally:
            test.doCleanups()

    @unittest.skipUnless(os.name == 'nt', 'native Windows')
    def test_posix_oauth_fixture_refuses_windows_before_setup(self):
        from test_connections import ConnectionsTests
        test = ConnectionsTests('runTest')
        with patch('test_connections.Connections', side_effect=AssertionError('unsafe fixture setup')):
            try:
                with self.assertRaises(unittest.SkipTest):
                    test.setUp()
            finally:
                test.doCleanups()

    @unittest.skipUnless(os.name == 'nt', 'native Windows')
    def test_age_rejects_untrusted_binary_parent_and_ancestor_acl(self):
        import shutil
        from finix import windows_native as win
        for target_kind, right in [('binary', 'WD'), ('parent', 'DC'), ('ancestor', 'DC')]:
            with self.subTest(target=target_kind), tempfile.TemporaryDirectory() as temp:
                root = core.private_dir(Path(temp) / 'root')
                parent = core.private_dir(root / 'bin')
                binary = parent / 'age.exe'
                shutil.copyfile(shutil.which('age'), binary)
                target = {'binary': binary, 'parent': parent, 'ancestor': root}[target_kind]
                result = subprocess.run(['icacls', str(target), '/grant', '*S-1-1-0:(' + right + ')'], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                with patch.dict(os.environ, PATH=str(parent)), patch('subprocess.Popen') as launch:
                    with self.assertRaises(OSError):
                        win.run_tool(['age', '--version'], capture_output=True)
                    launch.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'native Windows')
    def test_age_rejects_only_relative_path_entries(self):
        from finix import windows_native as win
        with patch.dict(os.environ, PATH='.;relative;'), patch('subprocess.Popen') as launch:
            with self.assertRaises(OSError):
                win.run_tool(['age', '--version'], capture_output=True)
            launch.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'native Windows')
    def test_every_age_launch_is_absolute_despite_cwd_poison(self):
        import shutil
        from finix import windows_native as win
        original = subprocess.Popen
        launched = []
        def checked(argv, *args, **kwargs):
            if Path(argv[0]).stem in {'age', 'age-keygen'}:
                self.assertTrue(Path(argv[0]).is_absolute(), argv)
                self.assertEqual(kwargs.get('executable'), argv[0])
                launched.append(Path(argv[0]).stem)
            return original(argv, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            for tool in ['age', 'age-keygen']:
                (base / (tool + '.exe')).write_bytes(b'not an executable; CWD poison')
            cwd = Path.cwd()
            try:
                os.chdir(base)
                with patch('subprocess.Popen', side_effect=checked):
                    recipient = core.keygen(base / 'identity')
                    encrypted = core.run_age(['-r', recipient], b'synthetic')
                    self.assertTrue(encrypted.startswith(b'age-encryption.org/v1'))
                    source = core.private_dir(base / 'source')
                    (source / 'config.yaml').write_bytes(b'synthetic')
                    result = core.backup(source, base / 'out', base / 'state', recipient,
                                         sqlite_temp_dir=core.private_dir(base / 'sqlite'))
                    core.restore(result['path'], base / 'identity', base / 'restored')
            finally:
                os.chdir(cwd)
        self.assertGreaterEqual(launched.count('age'), 3)
        self.assertEqual(launched.count('age-keygen'), 2)

    @unittest.skipUnless(os.name == 'nt', 'native Windows')
    def test_restore_rejects_ntfs_short_directory_alias(self):
        from finix import windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            stage = core.private_dir(Path(temp) / 'stage')
            existing = core.private_dir(stage / 'profile' / 'LongDirectoryName')
            get_short = win.api(win.K, 'GetShortPathNameW', win.W.DWORD, win.W.LPCWSTR, win.W.LPWSTR, win.W.DWORD)
            buffer = win.C.create_unicode_buffer(32768)
            self.assertTrue(get_short(str(existing), buffer, len(buffer)))
            alias = Path(buffer.value).name
            if alias.lower() == existing.name.lower():
                self.skipTest('8.3 generation disabled on test volume')
            with self.assertRaises(core.FinixError):
                streaming.restore_stream(archive_for(['LongDirectoryName/one', alias + '/two']), stage)

    def test_register_probe_without_manifest_never_starts_worker(self):
        from finix.integration import register
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            constants = SimpleNamespace(get_hermes_home=lambda: home,
                                        set_hermes_home_override=lambda value: None,
                                        reset_hermes_home_override=lambda value: None)
            ctx = SimpleNamespace(on_unload=Mock(), register_command=Mock(),
                                  register_cli_command=Mock(), register_telegram_handler=Mock(),
                                  get_config=lambda key, default=None: default)
            with patch.dict('sys.modules', hermes_constants=constants), \
                    patch('finix.extra_sources.trusted_root', return_value=home), \
                    patch('finix.scheduler.Worker') as worker:
                register(ctx)
                worker.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'native Windows')
    def test_foreign_profile_case_markers_are_rejected(self):
        from finix import windows_native as win
        for leaf, marker in [('foreign', 'CONFIG.YAML'), ('.HERMES', None)]:
            with self.subTest(leaf=leaf), tempfile.TemporaryDirectory() as temp:
                root = core.private_dir(Path(temp) / leaf)
                (root / 'PROFILES').mkdir()
                if marker:
                    (root / marker).write_bytes(b'x')
                with win.directory(root) as anchor, self.assertRaises(core.FinixError):
                    win.guard_tree(anchor, '', root_name=leaf)

    def test_legacy_plugin_case_exclusion(self):
        name = 'plugins/example/NODE_MODULES/dependency.js'
        self.assertEqual(core.legacy_allowed(name), os.name != 'nt')

    def test_platform_case_exclusion_backup_and_restore(self):
        for name in ['PROFILES/other/secret', 'Logs/output', 'project/Node_Modules/secret']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = core.private_dir(Path(temp) / 'root')
                target = root / name
                target.parent.mkdir(parents=True)
                target.write_bytes(b'x')
                with core.locked(root) as anchor:
                    inventory = core.scan(anchor)
                self.assertEqual(name in inventory, os.name != 'nt')
                stage = core.private_dir(Path(temp) / 'stage')
                if os.name == 'nt':
                    with self.assertRaises(core.FinixError):
                        streaming.restore_stream(archive_for([name]), stage)
                else:
                    streaming.restore_stream(archive_for([name]), stage)
                    self.assertTrue((stage / 'profile' / name).is_file())
