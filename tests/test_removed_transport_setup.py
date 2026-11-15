"""A retired transport refuses setup before changing retained verification."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from finix import core
from finix.setup_flow import SetupFlow
from finix.setup_errors import setup_error_code
from finix.workspace_drive import RemovedWorkspaceTransport


class RemovedTransportSetupTests(unittest.TestCase):
    def test_setup_keeps_receipt_settings_and_schedule(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP')) as tmp:
            base = Path(tmp)
            home, state = base / 'home', base / 'state'
            home.mkdir()
            state.mkdir()
            (home / 'config.yaml').write_text('{}', encoding='utf-8')
            cfg = {'storage': 'workspace', 'workspace_transport': 'skill',
                   'recipient': 'synthetic-public-recipient', 'interval_hours': 24,
                   'next_run': 12345}
            original_cfg = dict(cfg)
            settings = state / 'settings.json'
            settings.write_text(json.dumps(cfg), encoding='utf-8')
            receipt = state / 'setup-verification.json'
            receipt.write_bytes(b'{"historical":"preserve"}')
            before_settings, before_receipt = settings.read_bytes(), receipt.read_bytes()
            capture = Mock(side_effect=RemovedWorkspaceTransport())
            profile = SimpleNamespace(home=home, root=state, profile_id='b' * 64,
                config=lambda: cfg, directories=lambda: state, backup=capture)
            with patch('finix.setup_flow.core.locked', return_value=nullcontext()):
                with self.assertRaises(RemovedWorkspaceTransport):
                    SetupFlow(profile).check_and_run()
            self.assertTrue(receipt.exists(), 'A removed transport deleted the retained receipt')
            self.assertEqual(receipt.read_bytes(), before_receipt)
            self.assertEqual(settings.read_bytes(), before_settings)
            self.assertEqual(cfg, original_cfg)
            capture.assert_not_called()

    def test_removed_transport_has_a_closed_actionable_error(self):
        self.assertEqual(setup_error_code(RemovedWorkspaceTransport()),
                         'WORKSPACE_SKILL_TRANSPORT_REMOVED')

    def test_arbitrary_error_code_is_not_trusted(self):
        class UntrustedError(core.FinixError):
            code = 'WORKSPACE_SKILL_TRANSPORT_REMOVED'
        error = UntrustedError('PRIVATE-SENTINEL')
        self.assertEqual(setup_error_code(error), 'SETUP_CHECK_FAILED')


if __name__ == '__main__':
    unittest.main()
