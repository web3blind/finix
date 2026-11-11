"""Trusted CURRENT native package admission; run from a fresh assembled payload.

FINIX_NATIVE_TEST_SCOPE must be a new private synthetic SDK default, established
before SDK import. No gateway, key, archive recovery or maintenance is executed.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest

from finix import core, default_recovery_lifecycle as life
from finix.default_recovery import RecoveryError


class CurrentNativePayloadTests(unittest.TestCase):
    def setUp(self):
        self.scope = Path(os.environ['FINIX_NATIVE_TEST_SCOPE'])
        self.package = Path(life.__file__).parent.parent
        self.root = core.private_dir(self.scope / self._testMethodName)
        self.installed = core.private_dir(self.root / 'plugins/finix')
        self.names = sorted(p.relative_to(self.package).as_posix()
                            for p in self.package.rglob('*') if p.is_file())
        self.assertEqual(len(self.names), 72, 'Requires actual assembled native inventory')
        self.assertEqual(sum(n.startswith('finix/') and n.endswith('.py') for n in self.names), 43)
        for name in self.names:
            core.private_dir((self.installed / name).parent)
            shutil.copyfile(self.package / name, self.installed / name)
        core.atomic_json(self.root / 'config.yaml', {'plugins': {'enabled': ['finix']}})

    def test_actual_fresh_native_inventory_is_accepted(self):
        pinned = life._current_finix(self.root)
        expected = {'__init__.py', 'plugin.yaml', 'dashboard/plugin_api.py'}
        expected.update(n for n in self.names if n.startswith('finix/') and n.endswith('.py'))
        self.assertEqual(set(pinned), expected)
        for name, digest in pinned.items():
            self.assertEqual(digest, hashlib.sha256((self.package / name).read_bytes()).hexdigest())
        manifest = json.loads((self.package / 'RELEASE-MANIFEST.json').read_text())
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((self.installed / name).read_bytes()).hexdigest(), digest)

    def test_unknown_executable_members_remain_refused(self):
        for name in ('dashboard/other.py', 'unexpected.py', 'finix/unknown.py',
                     'dashboard/plugin_api.pyc', 'other/plugin_api.py'):
            with self.subTest(name=name):
                path = self.installed / name
                core.private_dir(path.parent)
                path.write_bytes(b'# not part of the trusted package\n')
                try:
                    with self.assertRaises(RecoveryError):
                        life._current_finix(self.root)
                finally:
                    path.unlink()

    def test_changed_approved_dashboard_leaf_remains_refused(self):
        (self.installed / 'dashboard/plugin_api.py').write_bytes(b'# changed\n')
        with self.assertRaises(RecoveryError):
            life._current_finix(self.root)

    def test_hardlinked_approved_dashboard_leaf_remains_refused(self):
        path = self.installed / 'dashboard/plugin_api.py'
        os.link(path, self.root / 'linked.py')
        with self.assertRaises((OSError, core.FinixError, RecoveryError)):
            life._current_finix(self.root)

    @unittest.skipUnless(os.name == 'nt', 'native Windows junction')
    def test_dashboard_reparse_directory_remains_refused(self):
        import subprocess
        real = self.root / 'real-dashboard'
        (self.installed / 'dashboard').rename(real)
        link = self.installed / 'dashboard'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(real)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises((OSError, core.FinixError, RecoveryError)):
                life._current_finix(self.root)
        finally:
            link.rmdir()

    def test_every_runtime_module_remains_digest_pinned(self):
        for name in self.names:
            if not (name.startswith('finix/') and name.endswith('.py')):
                continue
            with self.subTest(name=name):
                path = self.installed / name
                original = path.read_bytes()
                path.write_bytes(original + b'\n# changed leaf\n')
                try:
                    with self.assertRaises(RecoveryError):
                        life._current_finix(self.root)
                finally:
                    path.write_bytes(original)

    def test_disabled_plugin_does_not_gain_trust(self):
        core.atomic_json(self.root / 'config.yaml', {'plugins': {'enabled': []}})
        with self.assertRaises(RecoveryError):
            life._current_finix(self.root)

    def test_foreign_bundle_origin_does_not_gain_trust(self):
        old = life.__file__
        try:
            life.__file__ = str(self.scope / 'foreign-bundle/finix/default_recovery_lifecycle.py')
            with self.assertRaises((OSError, core.FinixError, RecoveryError)):
                life._current_finix(self.root)
        finally:
            life.__file__ = old


if __name__ == '__main__':
    unittest.main()
