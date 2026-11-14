"""Exact SDK-produced metadata through production admission; offline runner only."""
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

CONFIG = {'plugins': {'enabled': ['finix'], 'disabled': [],
                      'entries': {'finix': {'allow_tool_override': False}}},
          'auth': {'adopt_external_logins': False},
          'gateway': {'multiplex_profiles': False}, '_config_version': 52}


class StockSDKMetadataTests(unittest.TestCase):
    def setUp(self):
        # The prescribed runner installs its network/provider guard before imports.
        if not os.environ.get('FINIX_DEFAULT_TEST_SCOPE'):
            self.skipTest('use the guarded native acceptance run_tests.py')
        from finix import default_recovery_lifecycle
        self.lifecycle = default_recovery_lifecycle

    def test_exact_stock_config_production_inert_without_mutation(self):
        before = copy.deepcopy(CONFIG)
        self.assertTrue(self.lifecycle._inert(CONFIG, True))
        self.assertEqual(CONFIG, before)
        self.assertFalse(self.lifecycle._inert(CONFIG))

    def test_exact_version_optional_and_no_coercion(self):
        self.assertTrue(self.lifecycle._inert({'_config_version': 52}))
        self.assertTrue(self.lifecycle._inert({}))
        for value in (None, True, False, 0, 1, 51, 53, -1, 52.0, '52', '052', [], {}):
            with self.subTest(value=value):
                self.assertFalse(self.lifecycle._inert({'_config_version': value}, True))

    def test_exact_stock_maintenance_preflight_without_mutation(self):
        from finix import core
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
        self.assertEqual(root, Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])/'sdk-root')
        core.private_dir(root)
        core.atomic_json(root/'config.yaml', CONFIG)
        before = (root/'config.yaml').read_bytes()
        self.assertEqual(self.lifecycle.preflight(root, trusted_finix=True)[str(root)],
                         self.lifecycle.cold._digest(CONFIG))
        self.assertEqual((root/'config.yaml').read_bytes(), before)

    def test_entries_exact_type_keys_and_false_policy(self):
        invalid = [None, False, 0, '', [], {}, {'foreign': {'allow_tool_override': False}},
                   {'finix': None}, {'finix': []}, {'finix': {}},
                   {'finix': {'allow_tool_override': False, 'unknown': False}},
                   {'finix': {'allow_tool_override': False}, 'foreign': {'allow_tool_override': False}}]
        invalid += [{'finix': {'allow_tool_override': v}} for v in
                    (True, 0, 1, 'false', '', None, [], {})]
        invalid += [{'finix': {'allow_tool_override': False, k: v}} for k, v in
                    (('granted_capabilities', ['tools.override']), ('capabilities_consent', {}),
                     ('llm', {'allow_model_override': False}), ('allow_platform_actions', False))]
        for entries in invalid:
            config = copy.deepcopy(CONFIG)
            config['plugins']['entries'] = entries
            with self.subTest(entries=entries):
                self.assertFalse(self.lifecycle._inert(config, True))

    def test_policy_requires_existing_root_finix_authority(self):
        config = copy.deepcopy(CONFIG)
        del config['_config_version']
        self.assertTrue(self.lifecycle._inert(config, True))
        for enabled in ([], ['foreign'], ['finix', 'foreign'], ['finix', 'finix'],
                        None, False, 0, '', {}, ()):
            config = copy.deepcopy(CONFIG)
            config['plugins']['enabled'] = enabled
            for trusted in (False, True):
                with self.subTest(enabled=enabled, trusted=trusted):
                    self.assertFalse(self.lifecycle._inert(config, trusted))
        config = copy.deepcopy(CONFIG)
        del config['plugins']['enabled']
        self.assertFalse(self.lifecycle._inert(config, True))

    def test_sibling_marker_does_not_grant_finix_policy_authority(self):
        from finix import core
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
        core.private_dir(root)
        core.atomic_json(root/'config.yaml', CONFIG)
        sibling = core.private_dir(root/'profiles/stock-metadata-sibling')
        safe = {'_config_version': 52, 'plugins': {'enabled': [], 'disabled': []}}
        core.atomic_json(sibling/'config.yaml', safe)
        try:
            before = (sibling/'config.yaml').read_bytes()
            self.assertIn(str(sibling), self.lifecycle.preflight(root, trusted_finix=True))
            self.assertEqual((sibling/'config.yaml').read_bytes(), before)
            core.atomic_json(sibling/'config.yaml', CONFIG)
            before = (sibling/'config.yaml').read_bytes()
            with self.assertRaises(self.lifecycle.RecoveryError) as caught:
                self.lifecycle.preflight(root, trusted_finix=True)
            self.assertEqual(caught.exception.code, 'SIBLING_AUTORUN_NOT_SUPPORTED')
            self.assertEqual((sibling/'config.yaml').read_bytes(), before)
        finally:
            # Only the exact two freshly generated objects; never a live profile.
            (sibling/'config.yaml').unlink()
            sibling.rmdir()

    def test_maintenance_preflight_rejects_tampered_metadata_readonly(self):
        from finix import core
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
        core.private_dir(root)
        cases = []
        for value in (True, 52.0, '52', 53):
            config = copy.deepcopy(CONFIG)
            config['_config_version'] = value
            cases.append(config)
        for policy in ({'allow_tool_override': True}, {'allow_tool_override': 0},
                       {'allow_tool_override': False, 'granted_capabilities': ['tools.override']}):
            config = copy.deepcopy(CONFIG)
            config['plugins']['entries']['finix'] = policy
            cases.append(config)
        config = copy.deepcopy(CONFIG)
        config['plugins']['entries']['foreign'] = {'allow_tool_override': False}
        cases.append(config)
        try:
            for config in cases:
                with self.subTest(config=config):
                    core.atomic_json(root/'config.yaml', config)
                    before = (root/'config.yaml').read_bytes()
                    with self.assertRaises(self.lifecycle.RecoveryError) as caught:
                        self.lifecycle.preflight(root, trusted_finix=True)
                    self.assertEqual(caught.exception.code, 'DEFAULT_AUTORUN_NOT_SUPPORTED')
                    self.assertEqual((root/'config.yaml').read_bytes(), before)
        finally:
            core.atomic_json(root/'config.yaml', CONFIG)

    def test_metadata_does_not_weaken_active_or_unknown_refusals(self):
        for extra in ({'unknown': False}, {'hooks': {'startup': 'run'}},
                      {'skills': {'auto_load': ['foreign']}}, {'mcp_servers': {'x': {}}},
                      {'secrets': {'sources': ['x']}}, {'cron': {'provider': 'external'}},
                      {'platforms': {'telegram': {'enabled': True}}},
                      {'auth': {'adopt_external_logins': True}},
                      {'security': {'allow_lazy_installs': True}}):
            config = copy.deepcopy(CONFIG)
            config.update(extra)
            with self.subTest(extra=extra):
                self.assertFalse(self.lifecycle._inert(config, True))

    def test_actual_sdk_selection_and_enable_save_then_maintenance_preflight(self):
        from finix import core
        from hermes_constants import get_default_hermes_root
        from hermes_cli import config as sdk_config
        from hermes_cli import plugins_cmd_capabilities as consent
        from hermes_cli.plugin_capabilities import plugin_capability_granted
        from pm import publication
        sdk = Path(os.environ['FINIX_TEST_SDK_SOURCE']).resolve()
        self.assertEqual(Path(consent.__file__).resolve(), sdk/'hermes_cli/plugins_cmd_capabilities.py')
        self.assertEqual(Path(publication.__file__).resolve(), sdk/'pm/publication.py')
        root = get_default_hermes_root()
        self.assertEqual(root, Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])/'sdk-root')
        core.private_dir(root)
        initial = {k: v for k, v in CONFIG.items() if k not in ('plugins', '_config_version')}
        core.atomic_json(root/'config.yaml', initial)
        previous = (root/'config.yaml').read_bytes()
        # Execute real PM serializer, without dependency resolution/publication.
        with patch.object(publication, 'selection_snapshot', return_value={root/'config.yaml': previous}), \
             patch.object(publication, 'dependency_home_root', return_value=root), \
             patch.object(publication, 'candidate_members', return_value=[]), \
             patch.object(publication.PluginSelection, 'publish', side_effect=AssertionError('no PM publication')):
            prepared = publication.PluginSelection({'home': str(root), 'enabled': {'finix'}, 'disabled': set()})
        (root/'config.yaml').write_bytes(prepared.proposed)
        # Real enable producer: load defaults, write explicit denial, save while
        # stripping defaults and preserving the SDK schema stamp. No host starts.
        consent._set_plugin_entry_flag('finix', 'allow_tool_override', False)
        produced = self.lifecycle._config(root)
        self.assertEqual(produced, CONFIG)
        self.assertEqual(type(produced['_config_version']), int)
        self.assertFalse(plugin_capability_granted('finix', 'tools.override', config=produced))
        before = (root/'config.yaml').read_bytes()
        self.assertTrue(self.lifecycle._inert(produced, True))
        self.assertEqual(self.lifecycle.preflight(root, trusted_finix=True)[str(root)],
                         self.lifecycle.cold._digest(produced))
        self.assertEqual((root/'config.yaml').read_bytes(), before)
        with self.assertRaises(self.lifecycle.RecoveryError) as caught:
            self.lifecycle.preflight(root)
        self.assertEqual(caught.exception.code, 'DEFAULT_AUTORUN_NOT_SUPPORTED')
        self.assertEqual((root/'config.yaml').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
