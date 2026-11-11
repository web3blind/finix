"""Scoped standalone readiness; genuine end-to-end RED/GREEN lives in evidence."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from finix import core, recovery_activation as activation
from finix.recovery_import import RecoveryImportError

class SingleHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = core.private_dir(Path(self.temp.name)/'recovered')
        (self.target/'config.yaml').write_text('model: {default: fixture}\ngateway: {}\n')
    def identity(self):
        return {'pid':12345,'hermes_home':str(self.target),'state':'ready','served_profiles':[{'home':str(self.target),'profile_id':str(self.target)}]}
    def test_exact_ready_child(self):
        child=SimpleNamespace(pid=12345,poll=lambda:None)
        with patch('hermes_cli.gateway_runtime_start.spawn_unmanaged_gateway',return_value=child) as spawn, patch('hermes_cli.gateway_runtime_discovery.query_identify',return_value=self.identity()):
            self.assertEqual(activation._start_standalone(self.target),'ready')
            self.assertEqual(spawn.call_args.args,(self.target,))
        import yaml
        self.assertEqual(yaml.safe_load((self.target/'config.yaml').read_text())['gateway'],{'standalone':True,'multiplex_profiles':False})
    def rejected(self,bad):
        ticks=iter([None,0,0]); child=SimpleNamespace(pid=12345,poll=lambda:next(ticks))
        identity=self.identity(); identity.update(bad)
        with patch('hermes_cli.gateway_runtime_start.spawn_unmanaged_gateway',return_value=child), patch('hermes_cli.gateway_runtime_discovery.query_identify',return_value=identity):
            with self.assertRaisesRegex(RecoveryImportError,'SERVING_NOT_CONFIRMED'):
                activation._start_standalone(self.target)
    def test_wrong_pid(self): self.rejected({'pid':999})
    def test_wrong_home(self): self.rejected({'hermes_home':'other'})
    def test_pending(self): self.rejected({'state':'starting'})
    def test_wrong_route(self): self.rejected({'served_profiles':[]})

