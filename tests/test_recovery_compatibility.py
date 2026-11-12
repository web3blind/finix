"""A version/source fingerprint is not a public safe-import capability."""
from types import SimpleNamespace
import unittest
from finix import recovery_import as module


class CompatibilityContract(unittest.TestCase):
    def test_changed_host_source_is_closed_before_publication(self):
        host = SimpleNamespace(import_profile=lambda: None)
        with self.assertRaisesRegex(module.RecoveryImportError, 'HOST_IMPORT_CONTRACT_UNSUPPORTED'):
            module.verify_host_compatibility(host)

    def test_declared_sdk_version_does_not_claim_import_compatibility(self):
        self.assertEqual(module.DESKTOP_SDK_CONTRACT_VERSION, '0.23.0')
        self.assertFalse(hasattr(module, 'HOST_SOURCE_PINS'))
        with self.assertRaisesRegex(module.RecoveryImportError, 'HOST_IMPORT_CONTRACT_UNSUPPORTED'):
            module.verify_host_compatibility(SimpleNamespace(version='0.23.0'))
