"""Synthetic-profile contracts; run with the Hermes checkout on PYTHONPATH."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from finix import core
from finix.integration import Profile, command, register


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'profile'
        self.home.mkdir(mode=0o700)
        (self.home / 'config.yaml').write_text('test: synthetic\n')
        self.key = Path(self.tmp.name) / 'identity'
        self.recipient = core.keygen(self.key)
        self.profile = Profile(self.home)

    def test_backend_binding_and_cas(self):
        status = self.profile.status()
        self.assertEqual(Profile(self.home).profile_id, status['profile_id'])
        other = Profile(Path(self.tmp.name) / 'another-profile')
        self.assertNotEqual(other.profile_id, status['profile_id'])
        with self.assertRaises(core.FinixError):
            other.recipient(status['profile_id'], self.recipient, None)
        self.profile.recipient(status['profile_id'], self.recipient, None)
        with self.assertRaises(core.FinixError):
            self.profile.recipient(status['profile_id'], self.recipient, None)
        self.assertEqual(self.profile.status()['recipient'], self.recipient)

    def test_group_actor_cannot_mutate_or_view(self):
        for source, platform in [(None, 'telegram'), (SimpleNamespace(user_id='intruder'), 'telegram'),
                                  (SimpleNamespace(user_id='owner'), 'discord')]:
            with patch('finix.integration.current_profile', side_effect=AssertionError('must not resolve profile')):
                self.assertIn('denied', command('backup', owner_id='owner', owner_platform='telegram',
                                               source=source, platform=platform))
        with patch('finix.integration.current_profile', return_value=self.profile):
            result = command('status', owner_id='owner', owner_platform='telegram',
                             source=SimpleNamespace(user_id='owner'), platform='telegram')
            self.assertEqual(json.loads(result)['profile_id'], self.profile.profile_id)

    def test_actual_backup_schedule_retention(self):
        self.profile.recipient(self.profile.profile_id, self.recipient, None)
        self.assertEqual(self.profile.backup(scheduled=True), {'skipped': True})
        self.profile.configure('retention', '1')
        from finix.setup_flow import SetupFlow
        SetupFlow(self.profile).check_and_run()
        self.profile.configure('interval_hours', '1')
        first = self.profile.backup()
        second = self.profile.backup()
        self.assertFalse(Path(first['path']).exists())
        self.assertTrue(Path(second['path']).exists())
        self.assertEqual(Profile(self.home).backup(scheduled=True), {'skipped': True})
        target = Path(self.tmp.name) / 'restored'
        core.restore(second['path'], self.key, target)
        self.assertEqual((target / 'config.yaml').read_bytes(), (self.home / 'config.yaml').read_bytes())

    def test_legacy_schedule_cannot_bypass_setup_and_changes_pause_cron(self):
        from finix.setup_flow import SetupFlow
        self.profile.recipient(self.profile.profile_id, self.recipient, None)
        with self.assertRaises(core.FinixError):
            self.profile.configure('interval_hours', '1')
        first = SetupFlow(self.profile).check_and_run()['first_backup']
        self.profile.configure('interval_hours', '1')
        self.profile.configure('retention', '2')
        cfg = self.profile.config()
        cfg['next_run'] = 0
        core.atomic_json(self.profile.root / 'settings.json', cfg)
        with self.assertRaises(core.FinixError):
            self.profile.backup(scheduled=True)
        self.assertEqual(self.profile.config()['interval_hours'], 0)
        self.assertTrue(Path(first['path']).exists())

    def test_cloud_failure_preserves_old_local_copies(self):
        self.profile.recipient(self.profile.profile_id, self.recipient, None)
        first = self.profile.backup()
        self.profile.configure('retention', '1')
        self.profile.configure('storage', 'drive')
        cfg = self.profile.config()
        cfg['folder_id'] = 'synthetic-folder'
        core.atomic_json(self.profile.root / 'settings.json', cfg)
        from unittest.mock import Mock
        fake = Mock()
        fake.upload.side_effect = core.FinixError('corrupt readback')
        with patch('finix.drive.Drive', return_value=fake):
            with self.assertRaises(core.FinixError):
                self.profile.backup()
        self.assertTrue(Path(first['path']).exists())
        self.assertEqual(len(list((self.profile.root / 'copies').glob('*.age'))), 2)

    def test_real_sdk_registration_and_directory_load(self):
        from hermes_cli.plugins import PluginManifest, PluginManager, PluginContext
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name='finix', path=str(Path(__file__).parents[1]), source='user')
        module = manager._load_directory_module(manifest)
        ctx = PluginContext(manifest, manager)
        module.register(ctx)
        self.assertIn('finix', manager._plugin_commands)
        self.assertEqual(manager._tools if hasattr(manager, '_tools') else {}, {})
        from hermes_cli.plugins_dispatch import resolve_plugin_command_result
        self.assertIn('denied', resolve_plugin_command_result(manager._plugin_commands['finix']['handler']('status')))

    def test_actual_dashboard_http_cas(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        path = Path(__file__).parents[1] / 'dashboard' / 'plugin_api.py'
        spec = importlib.util.spec_from_file_location('finix_test_backend', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router)
        # The real host authenticates and binds profile scope before route invocation.
        with patch('hermes_constants.get_hermes_home', return_value=self.home):
            with TestClient(app) as client:
                response = client.get('/keygen.html')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, (path.parents[1] / 'dist' / 'keygen.html').read_bytes())
                self.assertIn('attachment', response.headers['content-disposition'])
                status = client.get('/status').json()
                payload = {'profile_id': status['profile_id'], 'recipient': self.recipient,
                           'expected_recipient': None}
                self.assertEqual(client.post('/recipient', json=payload).status_code, 200)
                self.assertEqual(client.get('/status').json()['recipient'], self.recipient)
                self.assertEqual(client.post('/recipient', json=payload).status_code, 409)
                payload['profile_id'] = 'wrong-backend'
                self.assertEqual(client.post('/recipient', json=payload).status_code, 409)
                payload['source_path'] = '/arbitrary'
                self.assertEqual(client.post('/recipient', json=payload).status_code, 422)


if __name__ == '__main__':
    unittest.main()
