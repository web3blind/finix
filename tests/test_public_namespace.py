"""Namespace hints retain filesystem identity using public Hermes constants only."""
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from finix import core, streaming


@unittest.skipUnless(os.name == 'posix', 'Native POSIX retained descriptor test')
class PublicNamespaceTests(unittest.TestCase):
    def probe(self, *, name: str | None = 'default', other=False):
        with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP')) as tmp:
            root = Path(tmp) / 'profile'
            alternate = Path(tmp) / 'alternate'
            root.mkdir()
            alternate.mkdir()
            public = ModuleType('hermes_constants')
            public.__dict__.update(
                profile_name_for_home=lambda home: name,
                get_default_hermes_root=lambda *, home: alternate if other else root,
                named_profile_home=lambda home: alternate if other else root)
            retired = ModuleType('hermes_cli.profiles')
            def forbidden(attr):
                raise AssertionError('Private profile API was accessed: ' + attr)
            retired.__dict__['__getattr__'] = forbidden
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.dict(sys.modules, {'hermes_constants': public,
                                              'hermes_cli.profiles': retired}):
                    return streaming._source_namespace(fd)
            finally:
                os.close(fd)

    def test_default_uses_public_root_with_retained_identity(self):
        self.assertEqual(self.probe(), 'default')

    def test_named_profile_uses_public_named_home(self):
        self.assertEqual(self.probe(name='fixture'), 'fixture')

    def test_different_default_identity_is_refused(self):
        with self.assertRaisesRegex(core.FinixError, 'HISTORY_SOURCE_BINDING_REQUIRED'):
            self.probe(other=True)

    def test_different_named_identity_is_refused(self):
        with self.assertRaisesRegex(core.FinixError, 'HISTORY_SOURCE_BINDING_REQUIRED'):
            self.probe(name='fixture', other=True)

    def test_namespace_is_not_case_repaired(self):
        with self.assertRaisesRegex(core.FinixError, 'HISTORY_SOURCE_BINDING_REQUIRED'):
            self.probe(name='MixedCase')

    def test_unknown_source_has_no_namespace_authority(self):
        self.assertIsNone(self.probe(name=None))


if __name__ == '__main__':
    unittest.main()
