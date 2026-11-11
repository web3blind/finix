"""Authenticated-host router contracts; all profiles/receipts are synthetic."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class SetupApiTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('finix_setup_test_api',
            Path(__file__).parents[1] / 'dashboard' / 'plugin_api.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        profile = SimpleNamespace(profile_id='synthetic-backend',
            config=lambda: {'recipient': None, 'storage': 'workspace'})
        calls = []
        state = {'profile_id': profile.profile_id, 'verified': False, 'stage': 'check',
                 'storage': 'workspace', 'interval_hours': 0}

        class Flow:
            def view(self):
                return dict(state)

            def check_and_run(self):
                calls.append('check')
                state.update(verified=True, stage='schedule')
                return self.view()

            def schedule(self, period):
                if not state['verified']:
                    raise module.FinixError('synthetic private provider details')
                calls.append(('schedule', period))
                state['interval_hours'] = int(period[:-1]) * (24 if period[-1] == 'd' else 1)
                return self.view()

        for name, value in [('current_profile', lambda: profile), ('setup_flow', lambda p: Flow())]:
            lease = patch.object(module, name, value)
            lease.start()
            self.addCleanup(lease.stop)
        app = FastAPI()
        app.include_router(module.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.state, self.calls, self.module = state, calls, module

    def test_check_failure_codes_are_closed_and_survive_status_refresh(self):
        import importlib
        rclone = importlib.import_module(self.module._package + '.rclone_storage')
        cases = [
            (__import__('sqlite3').DatabaseError('private SQLite path'), 'SETUP_SQLITE_FAILED'),
            (self.module.FinixError('Source changed during discovery'), 'SETUP_SOURCE_CHANGED'),
            (self.module.FinixError('Ciphertext readback mismatch'), 'SETUP_READBACK_FAILED'),
            (rclone.RcloneError('SHORT_DOWNLOAD'), 'SETUP_READBACK_FAILED'),
            (rclone.RcloneError('BINARY_MISSING'), 'SETUP_DEPENDENCY_UNAVAILABLE'),
            (rclone.RcloneError('CONFIG_UNAVAILABLE'), 'SETUP_CLOUD_CONFIG_UNAVAILABLE'),
            (rclone.RcloneError('PROVIDER_ERROR'), 'SETUP_CLOUD_FAILED'),
            (self.module.FinixError('NEED_SQLITE_TEMP_DIRECTORY'), 'SETUP_SQLITE_TEMP_REQUIRED'),
            (PermissionError('C:/private/user/data provider-secret'), 'SETUP_ACCESS_DENIED'),
            (ValueError('private provider stderr token=secret'), 'SETUP_CHECK_FAILED'),
        ]
        flow = self.module.setup_flow(None)
        body = {'profile_id': self.state['profile_id']}
        for error, code in cases:
            with self.subTest(code=code), patch.object(flow, 'check_and_run', side_effect=error), \
                    patch.object(self.module, 'setup_flow', lambda p: flow):
                failed = self.client.post('/setup/check', json=body)
                self.assertEqual(failed.status_code, 409)
                self.assertEqual(failed.json(), {'detail': code})
                status = self.client.get('/setup').json()
                self.assertEqual(status['last_error_code'], code)
                self.assertFalse(status['check_running'])
                self.assertNotIn('private', str(status))
                self.assertNotIn('secret', failed.text)
        self.client.post('/setup/check', json=body)
        self.assertIsNone(self.client.get('/setup').json()['last_error_code'])

    def test_public_readiness_is_no_spawn_and_strictly_allowlisted(self):
        import os
        import importlib
        import subprocess
        resolver = importlib.import_module(self.module._package + '.windows_native') if os.name == 'nt' else None
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('NO SPAWN')):
            response = self.client.get('/readiness')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertEqual(set(data), {'platform', 'code_version', 'tools'})
        self.assertEqual(data['code_version'], 'canonical-tools-v1')
        self.assertEqual(set(data['tools']), {'age', 'age-keygen', 'rclone'})
        for tool in data['tools'].values():
            self.assertEqual(set(tool), {'available', 'code'})
            self.assertIsInstance(tool['available'], bool)
            self.assertIn(tool['code'], {'READY', 'TOOL_MISSING', 'UNTRUSTED_BINARY', 'TOOL_CHECK_FAILED'})
        if resolver:
            for exc, code in [(FileNotFoundError('secret-path'), 'TOOL_MISSING'),
                              (PermissionError('secret-acl'), 'UNTRUSTED_BINARY'),
                              (RuntimeError('secret-runtime'), 'TOOL_CHECK_FAILED')]:
                with patch.object(resolver, 'find_tool', side_effect=exc):
                    denied = self.client.get('/readiness')
                self.assertNotIn('secret', denied.text)
                self.assertTrue(all(t == {'available': False, 'code': code}
                                    for t in denied.json()['tools'].values()))

    def test_setup_order_binding_and_exact_period_readback(self):
        client, state, calls = self.client, self.state, self.calls
        assert client.get('/setup').headers['cache-control'] == 'no-store'
        assert client.post('/setup/check', json={'profile_id': 'foreign'}).status_code == 409
        assert calls == []
        body = {'profile_id': state['profile_id']}
        denied = client.post('/setup/schedule', json={**body, 'period': '24h'})
        assert denied.status_code == 409
        assert 'private provider' not in denied.text
        assert client.post('/setup/check', json=body).json()['verified'] is True
        assert client.post('/setup/schedule', json={**body, 'period': '7d'}).json()['interval_hours'] == 168
        assert client.get('/setup').json()['interval_hours'] == 168
        assert calls == ['check', ('schedule', '7d')]

    def test_period_format_is_strict(self):
        for period in ['0h', '-1h', '1.5h', 'weekly', '24', ' 24h', '24H', '01h', '7d\n']:
            with self.subTest(period=period):
                response = self.client.post('/setup/schedule', json={
                    'profile_id': self.state['profile_id'], 'period': period})
                assert response.status_code == 422
                assert self.calls == []

    def test_extra_fields_and_key_before_cloud_guard(self):
        client, state, calls = self.client, self.state, self.calls
        assert client.post('/setup/check', json={'profile_id': state['profile_id'],
            'secret': 'synthetic'}).status_code == 422
        assert client.post('/connection', json={'profile_id': state['profile_id'],
            'ticket': 'synthetic', 'action': 'start'}).json()['detail'] == 'RECIPIENT_REQUIRED'
        assert calls == []

    def test_running_check_survives_client_wait_and_blocks_duplicate(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        client, state, module = self.client, self.state, self.module
        entered, release = threading.Event(), threading.Event()
        flow = module.setup_flow(None)
        check = flow.check_and_run

        def slow_check():
            entered.set()
            assert release.wait(5)
            return check()

        body = {'profile_id': state['profile_id']}
        with patch.object(flow, 'check_and_run', slow_check), patch.object(module, 'setup_flow', lambda p: flow):
            with ThreadPoolExecutor() as executor:
                pending = executor.submit(client.post, '/setup/check', json=body)
                try:
                    assert entered.wait(5)
                    assert client.get('/setup').json()['check_running'] is True
                    assert client.post('/setup/check', json=body).json()['detail'] == 'SETUP_CHECK_RUNNING'
                finally:
                    release.set()
                assert pending.result(timeout=5).json()['verified'] is True
        assert client.get('/setup').json()['check_running'] is False

    def test_legacy_local_default_is_not_implicit_cloud_choice(self):
        with patch.object(self.module, 'current_profile', lambda: SimpleNamespace(
                profile_id=self.state['profile_id'], config=lambda: {'storage': 'local'})):
            response = self.client.post('/setup/check', json={'profile_id': self.state['profile_id']})
        assert response.json()['detail'] == 'CLOUD_SELECTION_REQUIRED'
        assert self.calls == []
