"""CURRENT source integrity on disposable native files; no host or Apply.

Set FINIX_CURRENT_SOURCE_TEST_SCOPE to a fresh private directory. The loaded
package must also be a copied trusted native tree, not a shared checkout.
"""
import hashlib
import os
from pathlib import Path
import shutil
import unittest
import uuid

from finix import core, default_recovery_lifecycle as life


class CurrentSourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.root = core.private_dir(Path(os.environ['FINIX_CURRENT_SOURCE_TEST_SCOPE']) / uuid.uuid4().hex)
        self.installed = core.private_dir(self.root / 'plugins/finix')
        self.loaded = Path(life.__file__).parent.parent
        names = ['__init__.py', 'plugin.yaml', 'dashboard/plugin_api.py']
        names += ['finix/' + p.name for p in (self.loaded / 'finix').glob('*.py')]
        for name in names:
            target = self.installed / name
            core.private_dir(target.parent)
            shutil.copyfile(self.loaded / name, target)
        core.atomic_json(self.root / 'config.yaml', {'plugins': {'enabled': ['finix']}})
        self.pins = life._current_finix(self.root)
        self.source = self.installed / 'dashboard/plugin_api.py'

    def refused(self):
        with self.assertRaises((life.RecoveryError, core.FinixError, OSError)):
            life._current_finix(self.root)

    @unittest.skipIf(os.name == 'nt', 'POSIX mode policy')
    def test_current_source_group_writable_leaf_refused(self):
        self.source.chmod(0o664)
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX mode policy')
    def test_current_source_world_writable_leaf_refused(self):
        self.source.chmod(0o606)
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX mode policy')
    def test_current_source_writable_parent_refused(self):
        self.source.parent.chmod(0o775)
        self.refused()

    @unittest.skipUnless(os.name == 'nt', 'Native Windows link contract unchanged')
    def test_current_source_native_windows_multiple_links_refused(self):
        os.link(self.source, self.root / 'exported-source-link')
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX effective inode permissions')
    def test_current_source_nonowner_readonly_hardlink_preserves_pins(self):
        self.source.chmod(0o644)
        os.link(self.source, self.root / 'exported-source-link')
        self.assertEqual(life._current_finix(self.root), self.pins)

    @unittest.skipIf(os.name == 'nt', 'POSIX effective inode permissions')
    def test_current_source_group_writable_exported_hardlink_refused(self):
        self.source.chmod(0o664)
        os.link(self.source, self.root / 'exported-source-link')
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink')
    def test_current_source_symlink_leaf_refused(self):
        retained = self.root / 'retained-source'
        self.source.rename(retained)
        self.source.symlink_to(retained)
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink')
    def test_current_source_symlink_parent_refused(self):
        parent = self.source.parent
        retained = self.root / 'retained-dashboard'
        parent.rename(retained)
        parent.symlink_to(retained, target_is_directory=True)
        self.refused()
        parent.unlink()

    @unittest.skipIf(os.name == 'nt', 'POSIX loaded source mode policy')
    def test_current_source_loaded_package_is_guarded_too(self):
        from unittest.mock import patch
        copied = core.private_dir(self.root / 'loaded-package')
        for name in self.pins:
            target = copied / name
            core.private_dir(target.parent)
            shutil.copyfile(self.loaded / name, target)
        with patch.object(life, '__file__', str(copied / 'finix/default_recovery_lifecycle.py')):
            self.assertEqual(life._current_finix(self.root), self.pins)
            (copied / 'dashboard/plugin_api.py').chmod(0o664)
            self.refused()

    def test_current_source_private_copy_keeps_exact_hashes(self):
        expected = {name: hashlib.sha256((self.installed / name).read_bytes()).hexdigest()
                    for name in self.pins}
        self.assertEqual(life._current_finix(self.root), expected)
        self.assertEqual(expected, self.pins)

    @unittest.skipIf(os.name == 'nt', 'POSIX ordinary traversable directories')
    def test_current_source_ordinary_nonwritable_directories_admitted(self):
        self.installed.chmod(0o755)
        self.source.parent.chmod(0o755)
        self.source.chmod(0o644)
        self.assertEqual(life._current_finix(self.root), self.pins)

    @unittest.skipIf(os.name == 'nt', 'POSIX sticky package directory')
    def test_current_source_sticky_writable_package_parent_refused(self):
        self.source.parent.chmod(0o1777)
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX FIFO')
    def test_current_source_fifo_refused_without_blocking(self):
        self.source.unlink()
        os.mkfifo(self.source)
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor accounting')
    def test_current_source_descriptor_use_is_bounded_and_released(self):
        from unittest.mock import patch
        before = len(os.listdir('/proc/self/fd'))
        counts = []
        read = core.read_regular
        def measured(fd, limit=core.MAX_BYTES):
            counts.append(len(os.listdir('/proc/self/fd')))
            return read(fd, limit)
        with patch.object(core, 'read_regular', side_effect=measured):
            for _ in range(3):
                self.assertEqual(life._current_finix(self.root), self.pins)
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        self.assertLessEqual(max(counts), before + len(self.installed.parts) + 5)
        self.assertGreater(len(counts), len(self.pins))

    def test_current_source_byte_bound_refused(self):
        if os.name == 'nt':
            self.skipTest('POSIX source read bound; native Windows hashing unchanged')
        with self.source.open('r+b') as stream:
            stream.truncate(4*1024*1024+1)
        self.refused()

    def test_current_source_changed_bytes_refused(self):
        self.source.write_bytes(self.source.read_bytes() + b'\n# changed\n')
        self.refused()

    def test_current_source_unknown_executable_refused(self):
        (self.source.parent / 'unknown.py').write_bytes(b'raise AssertionError("not executed")\n')
        self.refused()


if __name__ == '__main__':
    unittest.main()
