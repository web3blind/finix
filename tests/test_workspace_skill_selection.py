"""Removal fails closed before helpers, journal reads or settings writes."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from finix import connections, integration, recovery_sources, workspace_drive


class WorkspaceSkillRemoval(unittest.TestCase):
    def profile(self, root, transport='skill'):
        cfg = {'storage': 'workspace', 'workspace_transport': transport,
               'workspace_account': 'a' * 64}
        return SimpleNamespace(home=root / 'home', root=root / 'state',
                               profile_id='b' * 64, config=lambda: cfg)

    def test_legacy_config_preserves_settings_journals_and_ciphertext(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self.profile(root)
            profile.root.mkdir()
            old = profile.root / 'workspace-skill'
            old.mkdir()
            fixtures = {profile.root / 'settings.json': json.dumps(profile.config()).encode(),
                        old / 'workspace.json': b'{"credential_authority":"google-workspace-skill:drive"}',
                        old / 'pending.json': b'{"phase":"uncertain"}',
                        old / 'backup.age': b'preserved ciphertext'}
            for path, data in fixtures.items():
                path.write_bytes(data)
            with patch.object(workspace_drive, 'WorkspaceDrive') as own:
                with self.assertRaises(workspace_drive.RemovedWorkspaceTransport) as caught:
                    integration.Profile.workspace(profile)
                own.assert_not_called()
            self.assertEqual(caught.exception.code, 'WORKSPACE_SKILL_TRANSPORT_REMOVED')
            self.assertIn('no automatic migration', str(caught.exception))
            self.assertEqual({p: p.read_bytes() for p in fixtures}, fixtures)
            self.assertFalse((profile.root / 'workspace').exists())

    def test_unknown_and_malformed_transports_never_fall_back(self):
        for value in ('unknown', '', None, False, [], {}):
            with self.subTest(value=value), patch.object(workspace_drive, 'WorkspaceDrive') as own:
                with self.assertRaises(workspace_drive.WorkspaceError) as caught:
                    integration.Profile.workspace(self.profile(Path('fixture'), value))
                self.assertEqual(caught.exception.code, 'UNSUPPORTED_TRANSPORT')
                own.assert_not_called()

    def test_supported_and_missing_selectors_keep_own_google_namespace(self):
        for explicit in (True, False):
            profile = self.profile(Path('fixture'), 'finix')
            if not explicit:
                del profile.config()['workspace_transport']
            with patch.object(workspace_drive, 'WorkspaceDrive') as own:
                integration.Profile.workspace(profile)
                own.assert_called_once_with(profile.home, profile.profile_id,
                    profile.root / 'workspace', account_digest='a' * 64)

    def test_removed_recovery_refuses_before_adapter_or_journal_access(self):
        profile = self.profile(Path('fixture'))
        profile.workspace = Mock(side_effect=AssertionError('must not construct adapter'))
        service = SimpleNamespace(add_source=Mock())
        with patch.object(recovery_sources, 'read_journal') as read:
            with self.assertRaises(workspace_drive.RemovedWorkspaceTransport):
                recovery_sources.register_cloud(service, profile)
            read.assert_not_called()
        profile.workspace.assert_not_called()
        service.add_source.assert_not_called()

    def test_scheduled_backup_refuses_before_pausing_schedule_or_capturing(self):
        from contextlib import nullcontext
        profile = self.profile(Path('fixture'))
        profile.directories = lambda: profile.root
        with patch.object(integration.core, 'locked', return_value=nullcontext()), patch.object(
                integration.core, 'atomic_json') as write, patch.object(integration.core, 'backup') as capture:
            with self.assertRaises(workspace_drive.RemovedWorkspaceTransport):
                integration.Profile.backup(profile, scheduled=True)
            write.assert_not_called()
            capture.assert_not_called()

    def test_old_authority_journal_is_not_adopted_by_own_google(self):
        profile = self.profile(Path('fixture'), 'finix')
        profile.workspace = lambda: SimpleNamespace(state=profile.root / 'workspace')
        service = SimpleNamespace(add_source=Mock())
        with patch.object(recovery_sources, 'read_journal', return_value={
                'credential_authority': 'google-workspace-skill:drive'}):
            recovery_sources.register_cloud(service, profile)
        service.add_source.assert_not_called()

    def test_supported_connection_choices_and_removed_start(self):
        manager = connections.Connections()
        profile = self.profile(Path('fixture'))
        self.assertEqual({p['id'] for p in manager.view(profile)['providers']},
                         {'workspace', 'yandex', 'onedrive', 'dropbox'})
        with patch.object(connections, '_google_runner') as own:
            with self.assertRaises(workspace_drive.RemovedWorkspaceTransport):
                manager._start(profile, 'workspace-skill', 'Finix', True, True)
            own.assert_not_called()
        self.assertEqual(manager.sessions, {})
        with self.assertRaises(workspace_drive.RemovedWorkspaceTransport):
            manager._apply(profile, {'public': {'provider': 'workspace-skill'}})

    def test_google_connection_still_uses_own_identity_without_subprocess(self):
        profile = self.profile(Path('fixture'), 'finix')
        manager = connections.Connections()
        runner = Mock(return_value={'account_id': 'synthetic-account'})
        with patch.object(workspace_drive, 'WorkspaceRunner', return_value=runner) as factory:
            manager._start(profile, 'workspace', 'Finix', True, True)
        factory.assert_called_once_with(profile.home)
        runner.assert_called_once_with('identity')
        session = manager.sessions[profile.profile_id]
        self.assertEqual(session['public']['status'], 'verified')
        self.assertIsNone(session['process'])
        self.assertEqual(profile.config()['workspace_transport'], 'finix')

    def test_only_rclone_providers_accept_bounded_authorization_url(self):
        url = 'http://127.0.0.1:53682/auth?state=synthetic'
        for provider in ('workspace', 'workspace-skill', 'unknown'):
            self.assertFalse(connections.safe_url(provider, url))
        for provider in ('yandex', 'onedrive', 'dropbox'):
            self.assertTrue(connections.safe_url(provider, url))


if __name__ == '__main__':
    unittest.main()
