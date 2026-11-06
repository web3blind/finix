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
