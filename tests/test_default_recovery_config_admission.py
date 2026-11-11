"""Pure admission tests; no SDK import, host, key, network or fixture writes."""
import ast
import os
import copy
from pathlib import Path
import unittest

# Extract the actual helper to keep native SDK bootstrap outside these unit tests.
SOURCE = Path(__file__).resolve().parents[1] / 'finix/default_recovery_lifecycle.py'
_tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
_scope = {}
exec(compile(ast.Module(body=[n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == '_inert'], type_ignores=[]), str(SOURCE), 'exec'), _scope)
inert = _scope['_inert']

# Exact minimal config mappings read from the existing native fixture, not rewritten there.
SIBLING = {'display': {'language': 'en'}, 'gateway': {'multiplex_profiles': False, 'standalone': False}, 'model': {}, 'plugins': {'enabled': []}}
ROOT = {'display': {'language': 'en'}, 'gateway': {'multiplex_profiles': True, 'standalone': False}, 'model': {}, 'plugins': {'enabled': ['finix']}, 'telemetry': {'shared_metrics': {'enabled': False, 'send': False, 'offer_version': 2}}}

class ConfigAdmissionTests(unittest.TestCase):
    def test_actual_root_and_sibling_config_admitted_without_changes(self):
        for config, trusted in ((ROOT, True), (SIBLING, False)):
            with self.subTest(trusted=trusted):
                before = copy.deepcopy(config)
                self.assertTrue(inert(config, trusted), 'DEFAULT_AUTORUN_NOT_SUPPORTED')
                self.assertEqual(config, before)

    def test_display_exact_types_and_unknown_fields_closed(self):
        for value in ({'language': False}, {'language': {}}, {'compact': 1}, {'compact': 'false'}, {'language': 'en', 'externalSource': 'x'}, {'hooks': []}, [], None):
            with self.subTest(value=value):
                self.assertFalse(inert({'display': value}))
        self.assertTrue(inert({'display': {'language': 'en', 'compact': False}}))

    def test_telemetry_only_exact_declined_producer_schema(self):
        for key, value in (('enabled', True), ('enabled', 0), ('send', True), ('send', 0), ('offer_version', True), ('offer_version', 3), ('endpoint', 'https://example.invalid'), ('externalSource', 'x')):
            config = copy.deepcopy(ROOT)
            config['telemetry']['shared_metrics'][key] = value
            with self.subTest(key=key, value=value): self.assertFalse(inert(config, True))
        for value in ({}, {'shared_metrics': {}}, {'shared_metrics': {'enabled': False, 'send': False}}, {'shared_metrics': ROOT['telemetry']['shared_metrics'], 'network': False}, None, []):
            with self.subTest(value=value): self.assertFalse(inert({'telemetry': value}))

    def test_executable_and_external_controls_remain_closed(self):
        for extra in ({'externalSource': 'x'}, {'scheduler': {}}, {'cron': {'provider': 'external'}}, {'hooks': {'startup': 'x'}}, {'mcp_servers': {'x': {}}}, {'secrets': {'externalSource': 'x'}}, {'plugins': {'enabled': ['other']}}, {'platforms': {'telegram': {'enabled': True}}}, {'kanban': {}}):
            config = copy.deepcopy(SIBLING)
            config.update(extra)
            with self.subTest(extra=extra): self.assertFalse(inert(config))
        self.assertFalse(inert(ROOT))  # Finix exemption remains root-only authority.

    def test_actual_pinned_sdk_decline_producer_ownership(self):
        # Parse, never import/initialize, the immutable SDK producer.
        sdk_source = os.environ.get('FINIX_TEST_SDK_SOURCE')
        if not sdk_source: self.skipTest('set FINIX_TEST_SDK_SOURCE to a pinned Hermes SDK checkout; see docs/TEST-SDK.md')
        sdk = Path(sdk_source)
        if not sdk.is_dir(): self.fail('FINIX_TEST_SDK_SOURCE must point to an existing pinned SDK checkout')
        module = ast.parse((sdk / 'hermes_cli/observability/shared_metrics_consent.py').read_text(encoding='utf-8'))
        nodes = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'set_answer' or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'OFFER_VERSION' for t in n.targets)]
        scope = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<pinned-sdk-set-answer>', 'exec'), scope)
        produced = {}
        scope['set_answer'](produced, False, False)
        self.assertEqual(produced, {'telemetry': ROOT['telemetry']})
        self.assertTrue(inert(produced))

if __name__ == '__main__': unittest.main()
