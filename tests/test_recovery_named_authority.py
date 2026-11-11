"""Focused native unittest guards; genuine registered CLI proof is external."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from finix import core, recovery_activation as activation
from finix.recovery_import import RecoveryImportError
from hermes_cli import profiles as host

class NamedHostTests(unittest.TestCase):
 def setUp(self):
  self.name='recovered-unit'; self.target=core.private_dir(host.get_profile_dir(self.name))
  (self.target/'config.yaml').write_text('gateway: {}\n')
  (self.target/'gateway.parked').write_text('finix-recovered-review-required\n')
  self.record=dict(name=self.name,target=self.target,digest=activation.profile_digest(self.target),started=False)
  self.service=SimpleNamespace(profile=self.target.parent/'source')
  import shutil
  self.addCleanup(lambda:shutil.rmtree(self.target))
 def start(self,owner):
  with patch('gateway.host_attach.host_gateway',return_value=owner), patch('gateway.control_socket.request_serve_profile_hot') as hot, patch.object(activation,'_start_standalone',return_value='ready') as launch:
   self.assertEqual(activation.start_imported(self.service,self.record,self.name)['served'],self.name)
   launch.assert_called_once_with(self.target); hot.assert_not_called()
 def test_named_owner(self): self.start(SimpleNamespace(home=self.service.profile,standalone=False))
 def test_standalone_owner(self): self.start(SimpleNamespace(home=self.service.profile,standalone=True))
 def test_absent_owner_explicit_policy(self): self.start(None)
 def test_unrelated_owner_no_hot_authority(self): self.start(SimpleNamespace(home=Path('/untrusted'),standalone=False))
 def test_wrong_target(self):
  self.record['target']=self.target.parent/'arbitrary'
  with patch.object(activation,'_start_standalone') as launch:
   with self.assertRaisesRegex(RecoveryImportError,'INVALID_NAME'): activation.start_imported(self.service,self.record,self.name)
   launch.assert_not_called()
 def test_bad_digest(self):
  self.record['digest']={}
  with patch('gateway.host_attach.host_gateway') as owner:
   with self.assertRaisesRegex(RecoveryImportError,'IMPORTED_PROFILE_CHANGED'): activation.start_imported(self.service,self.record,self.name)
   owner.assert_not_called()
 def test_unknown_hot_ack_is_not_fallback(self):
  with patch('gateway.host_attach.host_gateway',return_value=SimpleNamespace(home=self.target.parent.parent,standalone=False)), patch('gateway.control_socket.request_serve_profile_hot',return_value={'pending':True}), patch.object(activation,'_start_standalone') as launch:
   with self.assertRaisesRegex(RecoveryImportError,'SERVING_NOT_CONFIRMED'): activation.start_imported(self.service,self.record,self.name)
   launch.assert_not_called(); self.assertTrue((self.target/'gateway.parked').exists())
 def test_root_mux_explicit_not_ready_fallback(self):
  with patch('gateway.host_attach.host_gateway',return_value=SimpleNamespace(home=self.target.parent.parent,standalone=False)), patch('gateway.control_socket.request_serve_profile_hot',return_value={'error':'host multiplexer is not ready'}), patch.object(activation,'_start_standalone',return_value='ready') as launch:
   activation.start_imported(self.service,self.record,self.name); launch.assert_called_once_with(self.target)
