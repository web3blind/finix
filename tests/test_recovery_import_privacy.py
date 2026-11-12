"""Fail-closed import never changes native process temp or SDK filesystem state.

The old guarded-child activation tests are superseded: replacing Hermes shutil
or global tempfile state is not a supported publication capability. Real public
CLI parsing/root-guard checks live in test_reviewer_import_contract.py.
"""
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from finix import recovery_import as module


class ImportPrivacyContract(unittest.TestCase):
    def test_no_child_dispatch_or_temp_environment_mutation(self):
        before = dict(os.environ)
        tempdir = tempfile.tempdir
        with patch.object(subprocess, 'run') as run:
            with self.assertRaisesRegex(module.RecoveryImportError, 'HOST_IMPORT_CONTRACT_UNSUPPORTED'):
                module._host_import(SimpleNamespace(), Path('archive'), 'recovered',
                                    Path('target'), Path('stage'))
        run.assert_not_called()
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(tempfile.tempdir, tempdir)

    def test_legacy_child_cannot_mutate_sdk_shutil(self):
        try:
            from hermes_cli import profiles as host
        except ImportError:
            self.skipTest('SDK dependency closure not selected')
        shutil_module, move = host.shutil, host.shutil.move
        tempdir = tempfile.tempdir
        with self.assertRaisesRegex(module.RecoveryImportError, 'HOST_IMPORT_CONTRACT_UNSUPPORTED'):
            module._host_worker('missing', 'recovered', 'target', 'stage')
        self.assertIs(host.shutil, shutil_module)
        self.assertIs(host.shutil.move, move)
        self.assertEqual(tempfile.tempdir, tempdir)
