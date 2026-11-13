"""Explicit transport selection; never adopt another authorization namespace."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from finix import integration, workspace_drive, connections, recovery_sources


class WorkspaceSkillSelection(unittest.TestCase):
    def profile(self, transport=None):
        cfg = {'workspace_account': 'a' * 64}
        if transport is not None:
            cfg['workspace_transport'] = transport
        return SimpleNamespace(home=Path('fixture-home'), profile_id='b' * 64,
                               root=Path('fixture-state'), config=lambda: cfg)

    def selected(self, transport=None):
        def sdk(*args, **kwargs):
            return SimpleNamespace(kind='sdk', args=args, kwargs=kwargs)
        def skill(*args, **kwargs):
            return SimpleNamespace(kind='skill', args=args, kwargs=kwargs)
        module = SimpleNamespace(WorkspaceSkillDrive=skill)
        with patch.object(workspace_drive, 'WorkspaceDrive', sdk), patch.dict(
                sys.modules, {'finix.workspace_skill_transport': module}):
            return integration.Profile.workspace(self.profile(transport))

    def test_explicit_skill_transport_uses_separate_state_namespace(self):
        result = self.selected('skill')
        self.assertEqual(result.kind, 'skill')
        self.assertEqual(result.args[2], Path('fixture-state') / 'workspace-skill')
        self.assertEqual(result.kwargs['account_digest'], 'a' * 64)

    def test_existing_config_keeps_declared_finix_transport(self):
        result = self.selected()
        self.assertEqual(result.kind, 'sdk')
        self.assertEqual(result.args[2], Path('fixture-state') / 'workspace')

    def test_unknown_transport_refuses_without_fallback(self):
        with self.assertRaises(integration.core.FinixError):
            self.selected('unknown-provider')

    def test_skill_connection_is_explicit_and_checks_identity_without_oauth(self):
        profile = self.profile()
        manager = connections.Connections()
        calls = []
        def factory(home):
            self.assertEqual(home, profile.home)
            def request(operation):
                calls.append(operation)
                return {'account_id': 'synthetic-requester-123'}
            return request
        module = SimpleNamespace(WorkspaceSkillRunner=factory)
        with patch.dict(sys.modules, {'finix.workspace_skill_transport': module}):
            manager._start(profile, 'workspace-skill', 'Finix', True, True)
        self.assertEqual(calls, ['identity'])
        session = manager.sessions[profile.profile_id]
        self.assertEqual(session['public']['provider'], 'workspace-skill')
        self.assertEqual(session['public']['status'], 'verified')
        self.assertIsNone(session['process'])
        self.assertEqual(profile.config(), {'workspace_account': 'a' * 64})

    def test_skill_connection_rechecks_account_before_apply(self):
        profile = self.profile()
        manager = connections.Connections()
        current = ['synthetic-requester-123']
        def factory(home):
            return lambda operation: {'account_id': current[0]}
        module = SimpleNamespace(WorkspaceSkillRunner=factory)
        with patch.dict(sys.modules, {'finix.workspace_skill_transport': module}):
            manager._start(profile, 'workspace-skill', 'Finix', True, True)
            current[0] = 'different-synthetic-requester'
            with self.assertRaises(connections.ConnectionError) as caught:
                manager._apply(profile, manager.sessions[profile.profile_id])
        self.assertEqual(caught.exception.code, 'GOOGLE_ACCOUNT_CHANGED')
        self.assertEqual(profile.config(), {'workspace_account': 'a' * 64})

    def test_skill_recovery_reads_only_its_own_authority_and_state(self):
        cfg = {'storage': 'workspace', 'workspace_transport': 'skill',
               'workspace_account': 'a' * 64}
        authority = 'google-workspace-skill:drive'
        binding = {'provider': authority}
        adapter = SimpleNamespace(state=Path('fixture-state/workspace-skill'),
            credential_authority=authority, _binding=lambda: binding,
            descriptor=lambda row: row)
        profile = SimpleNamespace(profile_id='b' * 64, config=lambda: cfg,
                                  workspace=lambda: adapter)
        saved = {'records': [], 'profile_id': profile.profile_id, 'folder_id': 'fixture-folder',
                 'owners': ['synthetic-owner'], 'account': cfg['workspace_account'],
                 'binding': binding, 'credential_authority': authority}
        registered = []
        service = SimpleNamespace(activation='fixture-epoch', add_source=registered.append)
        with patch.object(recovery_sources, 'read_journal', return_value=saved) as read, patch.object(
                recovery_sources, 'committed_journal_source', side_effect=lambda **kw: kw):
            recovery_sources.register_cloud(service, profile)
        read.assert_called_once_with(adapter.state / 'workspace.json')
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0]['expected_binding']['credential_authority'], authority)
        cfg['workspace_transport'] = 'finix'
        with self.assertRaises(recovery_sources.RecoveryError):
            registered[0]['validate_record']({'size': 1, 'folder_id': 'fixture-folder',
                                              'owners': ['synthetic-owner']})
        cfg['workspace_transport'] = 'skill'
        saved['credential_authority'] = 'finix:google'
        registered.clear()
        with patch.object(recovery_sources, 'read_journal', return_value=saved):
            recovery_sources.register_cloud(service, profile)
        self.assertEqual(registered, [])

    def test_cli_accepts_explicit_skill_transport_and_owning_home(self):
        import argparse
        from finix.connect_cli import add_parser
        parser = argparse.ArgumentParser()
        add_parser(parser.add_subparsers(dest='command', required=True))
        args = parser.parse_args(['connect', 'workspace-skill', '--home', 'fixture-home',
                                  '--mode', 'local', '--confirm'])
        self.assertEqual(args.service, 'workspace-skill')
        self.assertEqual(args.home, 'fixture-home')
        self.assertTrue(args.confirm)
        self.assertFalse(args.open_browser)

    def test_skill_runtime_dependencies_and_payload_are_declared(self):
        import tomllib
        import yaml
        root = Path(__file__).resolve().parents[1]
        package = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
        manifest = yaml.safe_load((root / 'plugin.yaml').read_text(encoding='utf-8'))
        for name in ('google-api-python-client', 'google-auth-oauthlib'):
            self.assertTrue(any(row.startswith(name) for row in package['project']['dependencies']))
            self.assertTrue(any(row.startswith(name) for row in manifest['python_dependencies']))
        payload = package['tool']['setuptools']['data-files']
        for name in ('finix/workspace_skill_transport.py', 'finix/workspace_skill_worker.py'):
            self.assertIn(name, payload['share/finix/finix'])
        self.assertIn('docs/google-workspace-skill.md', payload['share/finix/docs'])

    def test_skill_transport_does_not_accept_rclone_authorization_urls(self):
        self.assertFalse(connections.safe_url('workspace-skill',
            'http://127.0.0.1:53682/auth?state=synthetic'))

    def test_skill_worker_preserves_origin_markers_but_not_ambient_secrets(self):
        import os
        from finix import workspace_skill_transport as transport
        markers = {'HERMES_GATEWAY_SESSION': 'synthetic-gateway',
                   'HERMES_SESSION_PLATFORM': 'desktop',
                   'HERMES_SINGLE_QUERY_SESSION': 'synthetic-query',
                   'HERMES_CRON_SESSION': '0',
                   'HERMES_INTERACTIVE': '0'}
        with patch.dict(os.environ, dict(markers, FINIX_TEST_PRIVATE_TOKEN='synthetic-secret')):
            env = transport.child_environment(Path('fixture-home'))
        for name, value in markers.items():
            self.assertEqual(env.get(name), value)
        self.assertNotIn('FINIX_TEST_PRIVATE_TOKEN', env)
        self.assertEqual(env['HERMES_HOME'], str(Path('fixture-home')))


if __name__ == '__main__':
    unittest.main()
