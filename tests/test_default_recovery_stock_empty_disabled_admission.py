"""Exact-empty plugin deny-list metadata must not grant activation authority."""
import ast
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'finix/default_recovery_lifecycle.py'
_tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
_scope = {}
exec(compile(ast.Module(body=[n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == '_inert'], type_ignores=[]), str(SOURCE), 'exec'), _scope)
inert = _scope['_inert']

# Exact safe projection of the stock native root's unchanged configuration.
CONFIG = {'auth': {'adopt_external_logins': False},
          'security': {'allow_lazy_installs': False},
          'display': {'language': 'en'},
          'telemetry': {'shared_metrics': {'enabled': False, 'send': False, 'offer_version': 2}},
          'plugins': {'enabled': ['finix'], 'disabled': []}}


class EmptyList(list):
    """Equality with [] is insufficient for literal producer metadata."""


class StockEmptyDisabledAdmissionTests(unittest.TestCase):
    def test_nonempty_coercible_and_malformed_disabled_remain_refused(self):
        for disabled in (['finix'], ['foreign'], ['foreign', 'foreign'], [''],
                         [None], [False], [0], [[]], [{}],
                         None, False, True, 0, 1, '', '[]', 'false', {},
                         {'foreign': False}, (), set(), EmptyList()):
            config = copy.deepcopy(CONFIG)
            config['plugins']['disabled'] = disabled
            for trusted in (False, True):
                with self.subTest(disabled=repr(disabled), kind=type(disabled).__name__, trusted=trusted):
                    self.assertFalse(inert(config, trusted))

    def test_empty_disabled_does_not_authorize_enabled_plugins(self):
        for enabled in (['foreign'], ['finix', 'foreign'], ['finix', 'finix'],
                        [''], [None], [False], [[]], None, False, 0, '', {}, ()):
            config = copy.deepcopy(CONFIG)
            config['plugins']['enabled'] = enabled
            for trusted in (False, True):
                with self.subTest(enabled=enabled, trusted=trusted):
                    self.assertFalse(inert(config, trusted))
        self.assertFalse(inert(CONFIG))

    def test_unknown_and_malformed_plugin_blocks_remain_refused(self):
        for plugins in (None, False, 0, '', [], (),
                        {'enabled': [], 'disabled': [], 'entries': {}},
                        {'enabled': [], 'disabled': [], 'disabled_by_update': {}},
                        {'enabled': [], 'disabled': [], 'unknown': False}):
            with self.subTest(plugins=plugins):
                self.assertFalse(inert({'plugins': plugins}, True))

    def test_empty_disabled_does_not_weaken_other_controls(self):
        for extra in ({'hooks': {'startup': 'run'}}, {'mcp_servers': {'foreign': {}}},
                      {'secrets': {'externalSource': 'foreign'}},
                      {'cron': {'provider': 'external'}},
                      {'skills': {'auto_load': ['foreign']}},
                      {'platforms': {'telegram': {'enabled': True}}},
                      {'auth': {'adopt_external_logins': True}},
                      {'security': {'allow_lazy_installs': 0}},
                      {'display': {'compact': 1}},
                      {'telemetry': {'shared_metrics': {'enabled': True, 'send': False, 'offer_version': 2}}},
                      {'model': {'default': []}},
                      {'gateway': {'standalone': 1}}, {'unknown': False}):
            config = copy.deepcopy(CONFIG)
            config.update(extra)
            with self.subTest(extra=extra):
                self.assertFalse(inert(config, True))

    def test_actual_stock_config_admitted_without_mutation(self):
        before = copy.deepcopy(CONFIG)
        self.assertTrue(inert(CONFIG, True), 'stock empty disabled metadata is not executable activation')
        self.assertEqual(CONFIG, before)

    def test_empty_disabled_is_optional_and_does_not_change_trust(self):
        for plugins in ({}, {'enabled': []}, {'disabled': []},
                        {'enabled': [], 'disabled': []}):
            for trusted in (False, True):
                with self.subTest(plugins=plugins, trusted=trusted):
                    self.assertTrue(inert({'plugins': plugins}, trusted))
        self.assertTrue(inert({'plugins': {'enabled': ['finix']}}, True))
        self.assertFalse(inert({'plugins': {'enabled': ['finix']}}))

    def test_genuine_sdk_selection_produces_exact_empty_metadata(self):
        sdk_source = os.environ.get('FINIX_TEST_SDK_SOURCE')
        if not sdk_source:
            self.skipTest('set FINIX_TEST_SDK_SOURCE to a pinned Hermes SDK checkout; see docs/TEST-SDK.md')
        sdk = Path(sdk_source).resolve()
        self.assertTrue(sdk.is_dir(), 'FINIX_TEST_SDK_SOURCE must exist')
        # Import the unchanged producer, not a reimplementation of its serializer.
        from pm import publication
        from hermes_yaml import roundtrip_yaml
        self.assertEqual(Path(publication.__file__).resolve(), sdk / 'pm/publication.py')
        home = Path.cwd() / 'unpublished-stock-selection'
        previous = json.dumps({k: v for k, v in CONFIG.items() if k != 'plugins'}).encode()

        def read(path):
            self.assertEqual(path, home / 'config.yaml')
            return previous

        # Only filesystem/topology inputs are modeled. Execute real __init__,
        # sorted lists, settle_record and YAML serialization; never publish or
        # resolve/install dependencies, scan the real home, or load plugin code.
        with patch.object(publication, 'selection_snapshot', return_value={home / 'config.yaml': previous}), \
             patch.object(publication, 'dependency_home_root', return_value=home), \
             patch.object(publication, 'read_bytes_or_none', side_effect=read), \
             patch.object(publication, 'candidate_members', return_value=[]), \
             patch.object(publication.PluginSelection, 'publish', side_effect=AssertionError('publication forbidden')) as publish:
            prepared = publication.PluginSelection({'home': str(home), 'enabled': {'finix'}, 'disabled': set()})
            publish.assert_not_called()
        self.assertEqual(prepared.previous, previous)
        produced = json.loads(json.dumps(roundtrip_yaml().load(prepared.proposed.decode('utf-8'))))
        self.assertEqual(produced, CONFIG)
        self.assertIs(type(produced['plugins']['disabled']), list)
        self.assertEqual(produced['plugins']['disabled'], [])
        self.assertTrue(inert(produced, True), 'genuine stock selection must be admitted without rewriting')
        self.assertFalse(inert(produced))


if __name__ == '__main__': unittest.main()
