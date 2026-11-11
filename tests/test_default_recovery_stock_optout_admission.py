"""Stock explicit opt-outs: pure admission, no SDK initialization or I/O."""
import ast
import copy
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'finix/default_recovery_lifecycle.py'
_tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
_scope = {}
exec(compile(ast.Module(body=[n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == '_inert'], type_ignores=[]), str(SOURCE), 'exec'), _scope)
inert = _scope['_inert']

CONFIG = {'gateway': {'standalone': True, 'multiplex_profiles': False},
          'plugins': {'enabled': ['finix']},
          'security': {'allow_lazy_installs': False},
          'auth': {'adopt_external_logins': False}}

class StockOptoutAdmissionTests(unittest.TestCase):
    def test_exact_failed_native_config_admitted_without_mutation(self):
        before = copy.deepcopy(CONFIG)
        self.assertTrue(inert(CONFIG, True), 'stock explicit opt-outs must be admitted')
        self.assertEqual(CONFIG, before)

    def test_each_explicit_false_optout_is_independently_admitted(self):
        for block, field in (('auth', 'adopt_external_logins'), ('security', 'allow_lazy_installs')):
            with self.subTest(block=block):
                self.assertTrue(inert({block: {field: False}}))

    def test_enabled_coercible_unknown_and_malformed_blocks_refused(self):
        for block, field in (('auth', 'adopt_external_logins'), ('security', 'allow_lazy_installs')):
            for value in (True, 0, 1, 'false', '', None, [], {}, 'False'):
                config = copy.deepcopy(CONFIG)
                config[block][field] = value
                with self.subTest(block=block, value=value): self.assertFalse(inert(config, True))
            for value in ({}, None, [], False, {field: False, 'unknown': False}, {'unknown': False}):
                config = copy.deepcopy(CONFIG)
                config[block] = value
                with self.subTest(block=block, malformed=value): self.assertFalse(inert(config, True))

    def test_optouts_do_not_grant_executable_or_shared_autorun_authority(self):
        for extra in ({'hooks': {'startup': 'run'}}, {'mcp_servers': {'x': {}}},
                      {'cron': {'provider': 'external'}}, {'secrets': {'source': 'x'}},
                      {'plugins': {'enabled': ['finix', 'foreign']}},
                      {'platforms': {'telegram': {'enabled': True}}}, {'unknown': False}):
            config = copy.deepcopy(CONFIG)
            config.update(extra)
            with self.subTest(extra=extra): self.assertFalse(inert(config, True))
        self.assertFalse(inert(CONFIG))

if __name__ == '__main__': unittest.main()
