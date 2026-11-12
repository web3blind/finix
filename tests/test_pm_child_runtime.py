"""PM worker selection remains pinned; Google no longer has a credential child.

Public Google HTTP/thread/namespace behavior is tested in the reviewer contract
against the real SDK. No Workspace skill or synthetic raw token store is loaded.
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from test_workspace_portability import NativeFixture
from finix.workspace_drive import WorkspaceError, trusted_runtime, _python_runtime

SDK=os.environ.get('FINIX_RUNTIME_SDK_ROOT')

@unittest.skipUnless(SDK,'requires explicitly selected real SDK fixture runner')
class PMChildRuntimeTests(NativeFixture):
    def setUp(self):
        super().setUp()
        self.sdk=Path(SDK)
        from pm.environments import selected_venv
        self.venv=selected_venv(self.sdk)
        self.script.write_text('raise AssertionError("pinning must not execute worker")')

    def test_changed_generation_fails_closed(self):
        from pm import environments
        with patch.object(environments,'running_from_selected_environment',return_value=False):
            with self.assertRaises(WorkspaceError):
                _python_runtime()

    def test_foreign_sdk_module_fails_closed(self):
        from pm import environments
        with patch.object(environments,'__file__',str(self.root/'environments.py')):
            with self.assertRaises(WorkspaceError):
                _python_runtime()

    def test_generation_changed_during_pinning_fails_closed(self):
        from pm import environments
        with patch.object(environments,'selected_venv',side_effect=[self.venv,self.root]):
            with self.assertRaises(WorkspaceError):
                with trusted_runtime(self.script,entry=self.script):
                    self.fail('changed generation was admitted')

    def test_google_public_request_needs_no_child_runtime(self):
        from test_reviewer_provider_contract import ProviderContract
        ProviderContract('test_real_sdk_public_refusals_namespace_and_thread_context').test_real_sdk_public_refusals_namespace_and_thread_context()
