"""Actual native-operation scheduling and consent-bound whole-helper controls."""
import os,sys,unittest,subprocess,shutil
from pathlib import Path
from unittest.mock import patch
from test_sqlite_family_transaction import FamilyTests
from finix import default_recovery as e, sqlite_family as f, core

class AdmissionTests(FamilyTests):
    def test_late_family_boundaries_preserve_conflict_and_refuse_reopen(self):
        for boundary in ('before-incoming','after-incoming','preterminal'):
            for same in (False,True):
                with self.subTest(boundary=boundary,same=same):
                    import uuid
                    self.directory=core.private_dir(self.scope/('case-'+uuid.uuid4().hex))
                    self.old_database();plan=self.prepare(self.capability());observed=[]
                    real=f._move;state=e.DefaultRecovery._state
                    def inject(this):
                        wal=self.root/'state.db-wal'
                        saved=this.directory/'sqlite-quarantine/state.db-wal'
                        wal.write_bytes(saved.read_bytes() if same else b'unknown-auxiliary')
                        observed.append(e._leaf(wal))
                        if same:
                            self.assertNotEqual(observed[-1]['identity'],e._leaf(saved)['identity'])
                    def move(this,source,target,leaf):
                        incoming=source.parent.name=='sqlite-staging'
                        if incoming and boundary=='before-incoming':inject(this)
                        result=real(this,source,target,leaf)
                        if incoming and boundary=='after-incoming':inject(this)
                        return result
                    def status(this,value,error=None):
                        if value=='published_offline' and boundary=='preterminal':inject(this)
                        return state(this,value,error)
                    with patch.object(f,'_move',move),patch.object(e.DefaultRecovery,'_state',status):
                        with self.assertRaises(e.RecoveryError) as caught:plan.apply(plan.consent())
                    self.assertEqual(caught.exception.code,'ROLLBACK_FAILED')
                    self.assertTrue(observed)
                    self.assertEqual(plan.status(),dict(state='rollback_failed',error='ROLLBACK_FAILED'))
                    self.assertEqual(e._leaf(self.root/'state.db-wal'),observed[0])
                    reopened=e.DefaultRecovery(plan.directory,plan.plan_digest)
                    for action in ('apply','rollback'):
                        with self.assertRaises(e.RecoveryError):getattr(reopened,action)(reopened.consent())
                        self.assertEqual(e._leaf(self.root/'state.db-wal'),observed[0])
                    print('NATIVE_LATE_CONFLICT',boundary,same,caught.exception.code,flush=True)
                    self.clean_family()

    def test_consent_whole_helper_pin_mutation_only_noncapture_source(self):
        if not os.environ.get('FINIX_PIN_CHILD'):
            candidate=Path(os.environ['FINIX_DEFAULT_TEST_CANDIDATE'])
            package=self.directory/'owned-package'
            shutil.copytree(candidate,package,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            if os.name!='nt':
                for path in [package,*package.rglob('*')]:path.chmod(0o700 if path.is_dir() else 0o600)
            for action in ('apply','rollback'):
                scope=core.private_dir(self.directory/('pin-'+action));core.private_dir(scope/'sdk-root')
                env=dict(os.environ,HERMES_HOME=str(scope/'sdk-root'),FINIX_DEFAULT_TEST_SCOPE=str(scope),FINIX_DEFAULT_TEST_CANDIDATE=str(package),FINIX_PIN_CHILD=action)
                code="""import os,sys,unittest
from pathlib import Path
root=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])/'sdk-root'
from hermes_constants import get_default_hermes_root,get_hermes_home
from hermes_cli.profiles import get_profile_dir
assert get_default_hermes_root()==root==get_profile_dir('default')
assert get_hermes_home()==root
p=Path(os.environ['FINIX_DEFAULT_TEST_CANDIDATE']);sys.path[:0]=[str(p),str(p/'tests')]
from test_sqlite_family_admission import AdmissionTests
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([AdmissionTests('test_consent_whole_helper_pin_mutation_only_noncapture_source')]))
sys.exit(not r.wasSuccessful())
"""
                result=subprocess.run([sys.executable,'-c',code],env=env,timeout=60)
                self.assertEqual(result.returncode,0,action)
            return
        self.old_database();plan=self.prepare(self.capability())
        p=plan._plan();pins=p['sqlite_families']['state.db']['capture_pins']
        self.assertIn('helper_module',pins)
        action=os.environ['FINIX_PIN_CHILD']
        if action=='rollback':self.assertEqual(plan.apply(plan.consent())['state'],'published_offline')
        consent=plan.consent();source=Path(f.__spec__.origin);original=source.read_bytes()
        before={n:e._leaf(self.root/n) if (self.root/n).exists() else None for n in f.NAMES}
        status=plan.status();journal=(plan.directory/'journal.json').read_bytes() if (plan.directory/'journal.json').exists() else None
        try:
            signature=('def '+('publish' if action=='apply' else 'restore')+'(engine,p,j):').encode()
            changed=original.replace(signature,signature+b'\n    raise RuntimeError("UNREVIEWED_HELPER")')
            self.assertNotEqual(changed,original);source.write_bytes(changed)
            with self.assertRaises(e.RecoveryError) as caught:getattr(plan,action)(consent)
            self.assertEqual(caught.exception.code,'PINNED_PACKAGE_CHANGED')
            with self.assertRaises(e.RecoveryError):e.DefaultRecovery(plan.directory,plan.plan_digest)
            self.assertEqual(plan.status(),status)
            self.assertEqual(before,{n:e._leaf(self.root/n) if (self.root/n).exists() else None for n in f.NAMES})
            self.assertEqual(journal,(plan.directory/'journal.json').read_bytes() if (plan.directory/'journal.json').exists() else None)
            print('WHOLE_HELPER_SOURCE_MUTATION_REFUSED',action,flush=True)
        finally:source.write_bytes(original)
        self.assertEqual(plan._plan(),p)
        if action=='apply':self.assertEqual(plan.apply(consent)['state'],'published_offline')
        self.assertEqual(plan.rollback(consent)['state'],'rolled_back')

    def test_controller_never_starts_either_generation_after_rollback_refusal(self):
        import uuid
        from contextlib import nullcontext
        from finix import default_recovery_lifecycle as live
        self.old_database();plan=self.prepare(self.capability());job=uuid.uuid4().hex
        directory=core.private_dir(live._job(self.root,job))
        record=dict(job=job,root_identity=e._identity(self.root),engine=plan._plan()['id'],
                    plan_digest=plan.plan_digest,consent=plan.consent(),owner={'fixture':'stopped'},
                    snapshot={'fixture':1})
        e._write(directory/'record.json',record)
        e._write(directory/'status.json',dict(state='registered',error=None))
        e._write(directory/'release.json',dict(record_digest=e._digest(record)))
        move=f._move;observed=[]
        def inject(this,source,target,leaf):
            result=move(this,source,target,leaf)
            if source.parent.name=='sqlite-staging':
                wal=self.root/'state.db-wal';wal.write_bytes(b'controller-unknown-leaf');observed.append(e._leaf(wal))
            return result
        with patch.object(live,'identify',return_value=record['owner']),patch.object(live,'drain'),patch.object(live,'preflight',return_value=record['snapshot']),patch.object(live,'writer_fence',side_effect=lambda root:nullcontext()),patch.object(live,'OwnedGateway') as start,patch.object(f,'_move',inject):
            with self.assertRaises(e.RecoveryError):live.run_job(self.root,job)
            start.assert_not_called()
            with self.assertRaises(e.RecoveryError):live.run_job(self.root,job)
            start.assert_not_called()
        self.assertEqual(e._leaf(self.root/'state.db-wal'),observed[0])
        self.assertEqual(plan.status(),dict(state='rollback_failed',error='ROLLBACK_FAILED'))
        status=e._load(directory/'status.json')
        self.assertEqual(status,dict(state='rollback_not_ready',error='TARGET_CHANGED'))
        history=e._load(directory/'history.json')
        self.assertTrue(any(v['state']=='rolling_back' and v['error']=='ROLLBACK_FAILED' for v in history))
        self.assertEqual(history[-1]['state'],'rollback_not_ready')
        from finix.default_recovery_api import DefaultRecoveryAPI
        self.assertEqual(DefaultRecoveryAPI._public(None,'a'*32,status),dict(id='a'*32,state='rollback_not_ready',error='DEFAULT_RECOVERY_FAILED'))
        with patch.object(live.ColdController,'_dispatch') as dispatch:
            self.assertEqual(live.ColdController(self.root).reopen(job),dict(job=job,state='rollback_not_ready'))
            dispatch.assert_not_called()
        print('CONTROLLER_MANUAL_CONFLICT_NO_NEW_OR_OLD_START',flush=True)

    def test_old_plan_missing_whole_helper_pin_fails_closed(self):
        self.old_database();plan=self.prepare(self.capability());p=plan._plan()
        p['sqlite_families']['state.db']['capture_pins'].pop('helper_module',None)
        e._write(plan.directory/'plan.json',p)
        with self.assertRaises(e.RecoveryError):e.DefaultRecovery(plan.directory,e._digest(p))
