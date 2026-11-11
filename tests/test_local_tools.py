"""No executable fixture is ever launched; discovery and trust only."""
import os
from pathlib import Path
import shutil
import uuid
from contextlib import contextmanager


@contextmanager
def fixture_root():
    from finix import windows_native as win
    root = win.private_dir(Path(os.environ['TMPDIR']) / ('finix-tools-' + uuid.uuid4().hex))
    try:
        yield str(root)
    finally:
        shutil.rmtree(root)
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == 'nt', 'native Windows discovery')
class LocalToolsTests(unittest.TestCase):
    def test_canonical_known_folder_does_not_follow_ambient_override(self):
        from finix import windows_native as win
        expected = win.local_appdata()
        with patch.dict(os.environ, {'LOCALAPPDATA': 'Z:\\untrusted-override',
                                     'USERPROFILE': 'Z:\\untrusted-profile', 'PATH': ''}):
            self.assertEqual(win.local_appdata(), expected)

    def test_canonical_lookup_failure_is_not_an_environment_fallback(self):
        from finix import windows_native as win
        with patch.object(win, 'local_appdata', side_effect=OSError('known folder unavailable')):
            with self.assertRaisesRegex(OSError, 'known folder unavailable'):
                win.find_tool('rclone')

    def test_host_without_localappdata_resolves_canonical_install_even_in_tools_cwd(self):
        from finix import windows_native as win
        with fixture_root() as temp:
            tools = win.private_dir(Path(temp) / 'Finix' / 'tools')
            binary = tools / 'rclone.exe'
            with win.regular(binary, create_new=True) as fd:
                os.write(fd, b'synthetic-not-executable')
            previous = Path.cwd()
            try:
                os.chdir(tools)
                with patch.dict(os.environ, {'PATH': ''}, clear=True), patch.object(
                        win, 'local_appdata', return_value=Path(temp), create=True):
                    self.assertEqual(win.find_tool('rclone'), str(binary))
            finally:
                os.chdir(previous)

    def test_installed_tools_resolve_without_path_and_remain_pinned(self):
        from finix import windows_native as win
        with fixture_root() as temp:
            tools = win.private_dir(Path(temp) / 'Finix' / 'tools')
            for name in ('age', 'age-keygen', 'rclone'):
                binary = tools / (name + '.exe')
                with win.regular(binary, create_new=True) as fd:
                    os.write(fd, b'synthetic-not-executable')
                with patch.dict(os.environ, {'LOCALAPPDATA': temp, 'PATH': ''}), patch.object(win, 'local_appdata', return_value=Path(temp)):
                    with win.trusted_executable(name) as found:
                        self.assertEqual(Path(found), binary)
                        with self.assertRaises(OSError):
                            binary.rename(binary.with_suffix('.moved'))
                        self.assertEqual(os.environ['PATH'], '')

    def test_rclone_execution_trust_rejects_untrusted_ancestor(self):
        from finix import windows_native as win
        with fixture_root() as temp:
            tools = win.private_dir(Path(temp) / 'tools')
            binary = tools / 'rclone.exe'
            with win.regular(binary, create_new=True) as fd:
                os.write(fd, b'synthetic-not-executable')
            with patch.object(win, 'trusted_handle', side_effect=lambda handle, **kw: not kw.get('ancestor', False)):
                self.assertFalse(win.trusted_binary(binary))

    def test_connections_and_storage_use_installed_rclone_without_path(self):
        from finix import windows_native as win
        from finix.connections import Connections
        from finix.integration import Profile
        from finix.rclone_storage import RcloneRunner
        with fixture_root() as temp:
            tools = win.private_dir(Path(temp) / 'Finix' / 'tools')
            binary = tools / 'rclone.exe'
            with win.regular(binary, create_new=True) as fd:
                os.write(fd, b'synthetic-not-executable')
            home = win.private_dir(Path(temp) / 'profile')
            profile = Profile(home, default_root=home)
            with patch.dict(os.environ, {'LOCALAPPDATA': temp, 'PATH': ''}), patch.object(win, 'local_appdata', return_value=Path(temp)):
                runner = RcloneRunner(home, home / 'unused-config')
                self.assertEqual(runner.binary, str(binary))
                with patch.object(win, 'popen', side_effect=RuntimeError('synthetic spawn blocked')) as spawn:
                    with self.assertRaisesRegex(RuntimeError, 'synthetic spawn blocked'):
                        Connections()._start(profile, 'yandex', 'Finix', True, True)
                    self.assertEqual(spawn.call_args.args[0][-1], str(binary))

    def test_installed_untrusted_tool_fails_closed_instead_of_path_fallback(self):
        from finix import windows_native as win
        with fixture_root() as temp:
            tools = win.private_dir(Path(temp) / 'Finix' / 'tools')
            with win.regular(tools / 'age.exe', create_new=True) as fd:
                os.write(fd, b'synthetic-not-executable')
            with patch.dict(os.environ, {'LOCALAPPDATA': temp, 'PATH': ''}), patch.object(win, 'local_appdata', return_value=Path(temp)), patch.object(win, 'trusted_handle', return_value=False):
                with self.assertRaises(PermissionError):
                    with win.trusted_executable('age'):
                        self.fail('untrusted tool admitted')
