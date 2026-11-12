"""Unsupported managed backend removal, not synthetic backend availability."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from finix import core, connector_drive
from finix.integration import Profile
from finix.recovery_sources import register_cloud

class ConnectorRemovalTests(unittest.TestCase):
    def test_no_auth_signing_transport_or_backend(self):
        for name in ('ManagedDrive', 'sdk_client', 'StorageTransport', 'profile_scope'):
            self.assertFalse(hasattr(connector_drive,name))
        self.assertFalse(hasattr(Profile, 'connector'))
        self.assertIn('unsupported',str(connector_drive.ConnectorError()))

    def test_legacy_config_and_journal_are_not_adopted_or_deleted(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            root = core.private_dir(Path(temp)/'private')
            settings = root/'settings.json'
            ledger = root/'connector.json'
            core.atomic_json(settings, {'storage':'connector-drive','connector_account':'old'})
            core.atomic_json(ledger, {'records':[{'id':'preserved'}]})
            before = (settings.read_bytes(),ledger.read_bytes())
            profile = SimpleNamespace(config=lambda:json.loads(settings.read_bytes()))
            service = SimpleNamespace(add_source=lambda source:self.fail('unsupported source advertised'))
            register_cloud(service,profile)
            self.assertEqual((settings.read_bytes(),ledger.read_bytes()),before)

if __name__ == '__main__':
    unittest.main()
