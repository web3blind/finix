"""Adapter tests use real private IO; SDK lifecycle is deliberately doubled."""
import argparse
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from finix import core


@unittest.skipUnless(os.environ.get('FINIX_PUBLIC_TEST_ROOT'), 'Use the isolated evidence runner; never use a personal SDK root')
class PublicTests(unittest.TestCase):
    @patch('finix.default_recovery_api.DEFAULT_APPLY_REVIEWED', False)
    def test_prepare_is_durable_and_never_registers(self):
        from finix import default_recovery_api as api
        root = core.private_dir(Path(os.environ['FINIX_PUBLIC_TEST_ROOT']))
        profile = SimpleNamespace(home=root, profile_id='a'*64)
        engine = SimpleNamespace(directory=root/'cache/finix/default-recovery'/('e'*32), plan_digest='d'*64,
            consent=lambda:{'root_identity':[1,2]},public_plan=lambda:dict(
                replacement_policy='profile-context-v1',replacement_roots=['memories','skills'],
                target_owned={},deletions={},kept_target_count=0,runtime_attempt=dict(
                    policy='runtime-attempt-preserve-v1',storage_format='opaque-container-v2',consent_notice='Failed startup custody',
                    retention='explicit-cleanup-only',keepSharedResources=True,
                    storage_cost='complete old copies + complete current copies and native originals')))
        challenge = {'consent': {'epoch':'epoch'}, 'runtime': {'authority_epoch':'old'}}
        controller = SimpleNamespace(confirmation=lambda e: copy.deepcopy(challenge), register=lambda *a: self.fail('prepare registered'))
        with patch.object(api, 'authority', return_value=root), patch.object(api, 'activation', return_value='binding'), patch.object(api.cold, 'verified_import', return_value=object()), patch.object(api.lifecycle, 'prepare_live', return_value=engine), patch.object(api.lifecycle, 'ColdController', return_value=controller):
            adapter = api.DefaultRecoveryAPI()
            before = list(root.rglob('*.json'))
            with self.assertRaises(api.RecoveryError): adapter.prepare(profile, object(), 'live', consent=False)
            self.assertEqual(before, list(root.rglob('*.json')))
            answer = adapter.prepare(profile, object(), 'live', consent=True)
            self.assertEqual(answer['state'], 'prepared')
            self.assertFalse(answer['apply_enabled'])
            with patch.object(api,'DEFAULT_APPLY_REVIEWED',True):
                self.assertTrue(adapter.prepare(profile,object(),'live',consent=True)['apply_enabled'])
            self.assertNotIn(str(root), json.dumps(answer))
            record = api.DefaultRecoveryAPI()._lookup(profile, answer['id'])
            self.assertEqual(record['challenge'], challenge)
            self.assertIsNone(record['job'])

    def test_apply_exact_consent_generation_and_durable_status(self):
        from finix import default_recovery_api as api
        from unittest.mock import Mock
        root=core.private_dir(Path(os.environ['FINIX_PUBLIC_TEST_ROOT']))
        profile=SimpleNamespace(home=root,profile_id='a'*64)
        opaque='b'*32
        challenge={'consent': {'epoch':'epoch'},'runtime': {'authority_epoch':'old'}}
        directory=core.private_dir(root/'cache/finix/default-api'/opaque)
        record=dict(version=1,id=opaque,profile_id=profile.profile_id,root_identity=api.cold._identity(root),activation='binding',engine='e'*32,digest='d'*64,challenge=challenge,job=None)
        api.cold._write(directory/'record.json',record)
        controller=Mock()
        controller.confirmation.return_value=copy.deepcopy(challenge)
        controller.register.return_value='c'*32
        controller.launch.return_value={'job':'c'*32,'state':'acknowledged','worker_pid':42}
        controller.status.return_value={'state':'ready','error':None,'identity':{'hermes_home':str(root)}}
        with patch.object(api,'DEFAULT_APPLY_REVIEWED',True), patch.object(api,'authority',return_value=root),patch.object(api,'activation',return_value='binding'),patch.object(api.cold,'DefaultRecovery'),patch.object(api.lifecycle,'ColdController',return_value=controller),patch.object(api.lifecycle,'identify',return_value=controller.status.return_value['identity']),patch.object(api.lifecycle,'_exact',return_value=True):
            a=api.DefaultRecoveryAPI()
            for consent,confirm in [(False,api.token(challenge)),(True,'wrong')]:
                with self.assertRaises(api.RecoveryError): a.apply(profile,opaque,confirm,consent=consent)
            controller.register.assert_not_called()
            controller.confirmation.return_value={'runtime':{'authority_epoch':'new'}}
            with self.assertRaises(api.RecoveryError): a.apply(profile,opaque,api.token(challenge),consent=True)
            controller.register.assert_not_called()
            controller.confirmation.return_value=copy.deepcopy(challenge)
            with patch.object(api,'activation',return_value='new-binding'):
                with self.assertRaises(api.RecoveryError): a.apply(profile,opaque,api.token(challenge),consent=True)
            result=a.apply(profile,opaque,api.token(challenge),consent=True)
            self.assertEqual(result,{'id':opaque,'state':'acknowledged','error':None})
            controller.register.assert_called_once()
            self.assertEqual(api.DefaultRecoveryAPI().status(profile,opaque)['state'],'ready')
            self.assertNotIn(str(root),json.dumps(a.status(profile,opaque)))
            for foreign in ('../escape','f'*32):
                with self.assertRaises(api.RecoveryError): a.status(profile,foreign)
            with self.assertRaises(api.RecoveryError): a.status(SimpleNamespace(home=root,profile_id='f'*64),opaque)
            with self.assertRaises(api.RecoveryError): a.reopen(profile,opaque,consent=False)
            controller.reopen.assert_not_called()
            controller.reopen.return_value={'state':'acknowledged'}
            self.assertEqual(a.reopen(profile,opaque,consent=True),{'id':opaque,'state':'acknowledged','error':None})
            controller.reopen.assert_called_once_with('c'*32)

    def test_cli_parser_and_http_schemas(self):
        from finix.plugin_cli import setup_parser
        parser=argparse.ArgumentParser()
        setup_parser(parser)
        args=parser.parse_args(['recovery','default-prepare','--request-id','b'*32,'--copy-id','x','--identity','C:/fixture/key','--consent-prepare'])
        self.assertEqual(args.recovery_action,'default-prepare')
        args=parser.parse_args(['recovery','default-apply','b'*32,'--confirmation','d'*64,'--confirm'])
        self.assertTrue(args.confirm)
        from finix.recovery_http import create_router
        router=create_router(lambda: None)
        paths={r.path for r in router.routes}
        for suffix in ('capabilities','prepare-auto','prepare','apply','reopen','status/{identifier}'):
            self.assertIn('/recovery/default/'+suffix,paths)

    @patch('finix.default_recovery_api.DEFAULT_APPLY_REVIEWED', False)
    def test_review_gate_blocks_apply_without_mutation(self):
        from finix import default_recovery_api as api
        with self.assertRaises(api.RecoveryError) as caught:
            api.DefaultRecoveryAPI().apply(None,'b'*32,'d'*64,consent=True)
        self.assertEqual(caught.exception.code,'DEFAULT_RECOVERY_REVIEW_REQUIRED')
        with self.assertRaises(api.RecoveryError) as caught:
            api.DefaultRecoveryAPI().reopen(None,'b'*32,consent=True)
        self.assertEqual(caught.exception.code,'DEFAULT_RECOVERY_REVIEW_REQUIRED')

    def test_inventory_includes_all_four_runtime_modules(self):
        import tomllib
        base=Path(__file__).resolve().parents[1]
        native=json.loads((base/'scripts/release/allowlist.json').read_text())
        wheel=tomllib.loads((base/'pyproject.toml').read_text())['tool']['setuptools']['data-files']['share/finix/finix']
        for name in ('default_recovery','default_recovery_lifecycle','default_recovery_worker','default_recovery_api'):
            self.assertIn('finix/'+name+'.py',native)
            self.assertIn('finix/'+name+'.py',wheel)

    @patch('finix.default_recovery_api.DEFAULT_APPLY_REVIEWED', False)
    def test_http_closed_schema_and_local_scope(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from finix.recovery_http import create_router
        from finix import default_recovery_api as api
        root=Path(os.environ['FINIX_PUBLIC_TEST_ROOT'])
        profile=SimpleNamespace(home=root,profile_id='a'*64)
        app=FastAPI()
        app.include_router(create_router(lambda:profile))
        with TestClient(app,client=('127.0.0.1',1234)) as client:
            response=client.post('/recovery/default/prepare',json={'profile_id':profile.profile_id,'recovery_job':'b'*32,'consent_prepare':False,'preview_contract':'default-preview-job-v1'})
            self.assertEqual(response.json()['detail'],'CONSENT_REQUIRED')
            response=client.post('/recovery/default/apply',json={'profile_id':profile.profile_id,'id':'b'*32,'confirmation':'d'*64,'consent_apply':True,'path':str(root)})
            self.assertEqual(response.status_code,422)
            response=client.post('/recovery/default/apply',json={'profile_id':profile.profile_id,'id':'b'*32,'confirmation':'d'*64,'consent_apply':True})
            self.assertEqual(response.json()['detail'],'DEFAULT_RECOVERY_REVIEW_REQUIRED')
            self.assertNotIn(str(root),response.text)
        with TestClient(app,client=('192.0.2.1',1234)) as client:
            self.assertEqual(client.get('/recovery/default/capabilities').status_code,403)

    def test_actual_sdk_root_fence_and_ordinary_cli_dispatch(self):
        from finix import default_recovery_api as api
        from finix.plugin_cli import run,setup_parser
        from contextlib import redirect_stdout
        import io
        root=Path(os.environ['FINIX_PUBLIC_TEST_ROOT'])
        profile=SimpleNamespace(home=root,profile_id='a'*64)
        self.assertEqual(api.authority(profile),root)
        foreign=core.private_dir(root/'profiles/foreign')
        with self.assertRaises(api.RecoveryError):api.authority(SimpleNamespace(home=foreign,profile_id='b'*64))
        parser=argparse.ArgumentParser()
        setup_parser(parser)
        with patch('finix.plugin_cli._recovery',return_value=17) as old, redirect_stdout(io.StringIO()):
            self.assertEqual(run(parser.parse_args(['recovery','list']),lambda:profile),17)
            old.assert_called_once()

    def test_genuine_encrypted_handoff_and_cold_plan(self):
        from finix import default_recovery_api as api
        from finix.integration import Profile
        root=Path(os.environ['FINIX_PUBLIC_TEST_ROOT'])
        core.atomic_json(root/'config.yaml',{})
        (root/'synthetic-note.md').write_text('Synthetic encrypted recovery fixture')
        profile=Profile(root,default_root=root)
        key=root.parent/'generated-test-key'
        recipient=core.keygen(key)
        captured=core.backup(root,profile.root/'copies',profile.root/'index',recipient,
                    sqlite_temp_dir=core.private_dir(root/'cache/finix/sqlite-temp'))
        service=api.new_service(profile)
        try:
            row=next(item for item in service.catalog(profile.profile_id,api.activation(profile))
                     if item['sha256']==captured['sha256'])
            destination=service.allocate_destination(profile.profile_id,api.activation(profile))
            job=service.start(row['id'],identity=key,destination=destination,
                              profile_id=profile.profile_id,activation=api.activation(profile))
            self.assertEqual(service.wait(job)['status'],'succeeded')
            def prepare(cap,root,**kw):return api.cold.prepare(cap,root,runtime_attempt_policy='runtime-attempt-preserve-v1',**kw)
            controller=SimpleNamespace(confirmation=lambda engine:{'consent':engine.consent(),'runtime':{'authority_epoch':'fixture-only'}})
            with patch.object(api.lifecycle,'prepare_live',side_effect=prepare),patch.object(api.lifecycle,'ColdController',return_value=controller):
                answer=api.DefaultRecoveryAPI().prepare(profile,service,job,consent=True)
            record=api.DefaultRecoveryAPI()._lookup(profile,answer['id'])
            engine=api.cold.DefaultRecovery(root/'cache/finix/default-recovery'/record['engine'],record['digest'])
            self.assertEqual(engine.status()['state'],'prepared')
            self.assertEqual((engine.directory/'payload/synthetic-note.md').read_text(),'Synthetic encrypted recovery fixture')
        finally:service.close()

    def test_public_adapter_exists(self):
        from importlib.util import find_spec
        self.assertIsNotNone(find_spec('finix.default_recovery_api'), 'public adapter missing')
