"""Exercise the real controller freezer, then CURRENT in an isolated interpreter.

Set FINIX_CONTROLLER_TEST_SCOPE to a fresh private disposable directory before
running. No SDK, gateway, maintenance registration, Apply or restore is invoked.
The trusted-runtime boundary is stopped before any production process launch.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import types
import unittest
import uuid
from unittest.mock import patch

SCOPE = Path(os.environ['FINIX_CONTROLLER_TEST_SCOPE']).absolute()
os.environ['HERMES_HOME'] = str(SCOPE)
os.environ.pop('HERMES_PROFILE', None)
sys.dont_write_bytecode = True
CHILD = r'''
import json, os, pathlib, sys
sys.dont_write_bytecode = True
root, package, deps = sys.argv[1:]
os.environ['HERMES_HOME'] = root
os.environ.pop('HERMES_PROFILE', None)
def audit(event, args):
    if event == 'open' and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
        raise PermissionError('READ_ONLY_ADMISSION')
    if event in ('os.mkdir','os.remove','os.rename','os.chmod','os.rmdir','subprocess.Popen') or event.startswith('socket.'):
        raise PermissionError('READ_ONLY_ADMISSION')
sys.addaudithook(audit)
sys.path[:0] = [package, deps]
from finix.default_recovery_lifecycle import _current_finix
try:
    pins = _current_finix(pathlib.Path(root))
    print(json.dumps({'ok': True, 'pins': pins}))
except Exception as error:
    print(json.dumps({'ok': False, 'exception': type(error).__name__,
                      'code': getattr(error, 'code', None), 'errno': getattr(error, 'errno', None)}))
'''

ALLOWED_COMMAND = None

def audit(event, args):
    if event.startswith('socket.'):
        raise PermissionError('OFFLINE_FREEZER_TEST')
    if event == 'subprocess.Popen' and args[1] != ALLOWED_COMMAND:
        raise PermissionError('ONLY_READ_ONLY_ADMISSION_CHILD_ALLOWED')
sys.addaudithook(audit)

# Guards and root selectors precede every candidate import.
from finix import core, default_recovery_lifecycle as life, workspace_drive
import yaml


class FreezeComplete(Exception):
    pass


class ControllerPackageFreezeTests(unittest.TestCase):
    def setUp(self):
        self.root = core.private_dir(SCOPE / uuid.uuid4().hex)
        self.installed = core.private_dir(self.root / 'plugins/finix')
        self.source = Path(life.__file__).resolve().parent.parent
        names = ['__init__.py', 'plugin.yaml', 'dashboard/plugin_api.py']
        names += ['finix/' + p.name for p in sorted((self.source / 'finix').glob('*.py'))]
        for name in names:
            life.cold._copy(self.source / name, self.installed / name)
        core.atomic_json(self.root / 'config.yaml', {'plugins': {'enabled': ['finix']}})
        self.pins = life._current_finix(self.root)
        self.job = uuid.uuid4().hex
        self.directory = core.private_dir(life._job(self.root, self.job))
        life.cold._write(self.directory / 'record.json', {'job': self.job})
        self.controller = life.ColdController.__new__(life.ColdController)
        self.controller.root = self.root
        self.package = self.directory / 'package'

    def freeze(self):
        # Only the post-freeze runtime launch seam is replaced. All enumeration,
        # existing-target checks, private parent construction and copies are real.
        constants = types.ModuleType('hermes_constants')
        constants.__file__ = str(self.root / 'unused-sdk/hermes_constants.py')
        with patch.dict(sys.modules, {'hermes_constants': constants}), \
                patch.object(workspace_drive, 'trusted_runtime', side_effect=FreezeComplete), \
                patch.object(life.subprocess, 'Popen', side_effect=AssertionError('FORBIDDEN_PRODUCT_LAUNCH')) as launch:
            try:
                self.controller._dispatch(self.job)
            except FreezeComplete:
                pass
            else:
                self.fail('Actual dispatch must reach the guarded launch boundary')
            finally:
                launch.assert_not_called()

    def probe(self):
        global ALLOWED_COMMAND
        command = [sys.executable, '-I', '-B', '-c', CHILD,
                   str(self.root), str(self.package), str(Path(yaml.__file__).parent.parent)]
        ALLOWED_COMMAND = subprocess.list2cmdline(command) if os.name == 'nt' else command
        try:
            child = subprocess.run(command, capture_output=True, text=True, timeout=30)
        finally:
            ALLOWED_COMMAND = None
        self.assertEqual(child.returncode, 0, child.stderr)
        return json.loads(child.stdout)

    def test_actual_freezer_satisfies_detached_current_admission(self):
        self.freeze()
        actual = self.probe()
        self.assertTrue(actual['ok'], actual)
        self.assertEqual(actual['pins'], self.pins)
        adapter = self.package / 'dashboard/plugin_api.py'
        self.assertEqual(adapter.read_bytes(), (self.source / 'dashboard/plugin_api.py').read_bytes())
        self.assertEqual(adapter.stat().st_nlink, 1)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(adapter.parent.stat().st_mode), 0o700)
        else:
            from finix import windows_native as win
            with win.directory(adapter.parent) as anchor:
                self.assertTrue(win.trusted_handle(anchor.handle, private=True))

    def test_identical_frozen_package_is_reused_without_replacing_leaves(self):
        self.freeze()
        paths = sorted(p for p in self.package.rglob('*') if p.is_file())
        before = {str(p): (p.stat().st_ino, p.read_bytes()) for p in paths}
        with patch.object(life.cold, '_copy', side_effect=AssertionError('NO_RECOPY')):
            self.freeze()
        self.assertEqual(before, {str(p): (p.stat().st_ino, p.read_bytes()) for p in paths})
        self.assertTrue(self.probe()['ok'])

    def test_changed_frozen_dashboard_is_refused_before_launch(self):
        self.freeze()
        target = self.package / 'dashboard/plugin_api.py'
        target.write_bytes(b'# changed frozen adapter\n')
        with self.assertRaisesRegex(life.RecoveryError, 'PINNED_PACKAGE_CHANGED'):
            self.freeze()
        self.assertEqual(target.read_bytes(), b'# changed frozen adapter\n')

    def test_installed_unknown_python_and_forged_cache_remain_refused(self):
        self.freeze()
        for name in ('unexpected.py', 'dashboard/unknown.py', 'dashboard/plugin_api.pyc'):
            with self.subTest(name=name):
                target = self.installed / name
                target.write_bytes(b'not approved executable bytes')
                try:
                    self.assertFalse(self.probe()['ok'])
                finally:
                    target.unlink()

    def test_changed_installed_dashboard_remains_refused(self):
        self.freeze()
        (self.installed / 'dashboard/plugin_api.py').write_bytes(b'# different digest\n')
        self.assertFalse(self.probe()['ok'])

    @unittest.skipUnless(os.name == 'nt', 'Native Windows regular-file link admission')
    def test_hardlinked_dashboard_remains_refused(self):
        self.freeze()
        os.link(self.installed / 'dashboard/plugin_api.py', self.root / 'second-link')
        self.assertFalse(self.probe()['ok'])

    def real_cache(self):
        import py_compile
        source = self.installed / 'finix/default_recovery_api.py'
        core.private_dir(source.parent / '__pycache__')
        cache = Path(py_compile.compile(str(source), doraise=True))
        self.assertTrue(self.probe()['ok'], 'Genuine compiler cache must be accepted first')
        return cache

    def test_hardlinked_source_cache_remains_refused(self):
        self.freeze()
        cache = self.real_cache()
        os.link(cache, self.root / 'second-cache-link')
        self.assertFalse(self.probe()['ok'])

    def test_timestamp_matching_forged_source_cache_remains_refused(self):
        import marshal
        self.freeze()
        cache = self.real_cache()
        forged = compile('raise RuntimeError("must never execute")',
                         str(self.installed / 'finix/default_recovery_api.py'), 'exec')
        cache.write_bytes(cache.read_bytes()[:16] + marshal.dumps(forged))
        self.assertFalse(self.probe()['ok'])

    @unittest.skipIf(os.name == 'nt', 'POSIX cache ancestor mode')
    def test_unsafe_source_cache_ancestor_remains_refused(self):
        self.freeze()
        cache = self.real_cache()
        cache.parent.chmod(0o775)
        self.assertFalse(self.probe()['ok'])


if __name__ == '__main__':
    unittest.main()
