"""Native, owned-root-only tests. Runner must fence SDK before collection."""
import os
from pathlib import Path
from contextlib import closing
import unittest

class DefaultRoleTests(unittest.TestCase):
    def test_named_import_cannot_be_promoted_by_changing_the_selector(self):
        from hermes_constants import get_default_hermes_root
        from hermes_cli.profiles import get_profile_dir
        scope = Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        root = get_default_hermes_root()
        self.assertTrue(root.is_relative_to(scope))
        named = get_profile_dir('recovered-test')
        self.assertNotEqual(root, named)
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('finix.default_recovery'),
                             'Missing true-default engine: named import retains named role')
        from finix import default_recovery as engine
        with self.assertRaises(engine.RecoveryError) as caught:
            engine.prepare(object(), named, journal_root=root/'cache/finix/default-recovery',
                           intent='replace-real-default')
        self.assertEqual(caught.exception.code, 'INVALID_CAPABILITY')
        self.assertFalse((root/'cache/finix/default-recovery').exists())
        with self.assertRaises(engine.RecoveryError):
            engine.DefaultRecovery(scope,'0'*64)

class VerifiedPlanTests(unittest.TestCase):
    def test_verified_restore_prepares_default_data_not_shared_root(self):
        import time, uuid, sqlite3
        from finix import core, default_recovery as engine
        from finix.recovery import RecoveryService
        from hermes_constants import get_default_hermes_root
        from hermes_cli.profiles import get_profile_dir
        root = get_default_hermes_root()
        scope = Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        self.assertEqual(root, scope/'sdk-root')
        core.private_dir(root)
        source = core.private_dir(scope/'source')
        (source/'config.yaml').write_text('{}\n')
        (source/'note.txt').write_text('restored-note')
        core.private_dir(source/'installs'); (source/'installs/source-tool').write_text('archived-tool')
        with sqlite3.connect(source/'state.db') as db:
            db.execute('create table synthetic(value text)')
            db.execute("insert into synthetic values ('restored')")
        (root/'note.txt').write_text('old-note')
        core.private_dir(root/'memories'); (root/'memories/old-only.md').write_text('old memory')
        core.private_dir(root/'skills/old-only'); (root/'skills/old-only/SKILL.md').write_text('old skill')
        with closing(sqlite3.connect(root/'state.db')) as old_db:
            old_db.execute('create table old_default(value text)')
            old_db.execute("insert into old_default values ('old-root-data')")
            old_db.commit()
        core.private_dir(root/'profiles/sentinel'); (root/'profiles/sentinel/data').write_text('other-profile')
        core.private_dir(root/'installs'); (root/'installs/host').write_text('host-install')
        core.private_dir(root/'tools');(root/'tools/sentinel').write_text('protected tool')
        core.private_dir(root/'plugins/finix');(root/'plugins/finix/control').write_text('trusted control')
        (root/'unknown-local-context').write_text('retained unknown')
        core.private_dir(root/'hermes-agent'); (root/'hermes-agent/code').write_text('trusted-code')
        key = scope/'generated.key'; recipient=core.keygen(key)
        out, index=scope/'cipher',scope/'index'
        core.backup(source,out,index,recipient,sqlite_temp_dir=core.private_dir(source/'cache/finix/sqlite'))
        service=RecoveryService(profile_id='default',profile_root=root,activation='synthetic-epoch',protected_roots=[])
        self.addCleanup(service.close)
        from finix.recovery import CommittedSource
        row=core.load_state(index)['backups'][0]
        cipher=out/row['name']; row=dict(row,size=cipher.stat().st_size)
        def transfer(record,output,cancel):
            with cipher.open('rb') as stream:
                while chunk:=stream.read(65536): output.write(chunk)
        service.add_source(CommittedSource('synthetic-local','fixture',lambda:[row],transfer))
        selected=service.catalog('default','synthetic-epoch')[0]
        job=service.start(selected['id'],identity=key,destination=service.allocate_destination('default','synthetic-epoch'),profile_id='default',activation='synthetic-epoch')
        deadline=time.monotonic()+30
        while service.status(job)['status']=='running' and time.monotonic()<deadline: time.sleep(.02)
        self.assertEqual(service.status(job)['status'],'succeeded',service.status(job))
        self.assertTrue(callable(getattr(engine,'verified_import',None)), 'No verified import capability factory')
        cap=engine.verified_import(service,job,profile_id='default',activation='synthetic-epoch',name='recovered-test')
        self.assertNotEqual(get_profile_dir('recovered-test'),root)
        journal=root/'cache/finix/default-recovery'
        plan=engine.prepare(cap,root,journal_root=journal,intent='replace-real-default')
        self.assertEqual(plan.status()['state'],'prepared')
        public=plan.public_plan()
        self.assertIn('memories/old-only.md',public['deletions'])
        self.assertIn('skills/old-only/SKILL.md',public['deletions'])
        self.assertIn('unknown-local-context',public['kept_target'])
        self.assertIn('tools',public['kept_target'])
        self.assertEqual(public['kept_target_count'],len(public['kept_target']))
        self.assertEqual((root/'note.txt').read_text(),'old-note')
        self.assertEqual(plan.public_plan()['role'],'default')
        self.assertGreaterEqual(plan.public_plan()['retained_source_shared_files'],1)
        self.assertEqual((root/'installs/host').read_text(),'host-install')
        self.assertEqual((root/'profiles/sentinel/data').read_text(),'other-profile')
        self.assertTrue(callable(getattr(plan,'apply',None)),'No transactional apply engine')
        before=(root/'note.txt').read_bytes()
        with self.assertRaises(engine.RecoveryError) as error: plan.apply(None)
        self.assertEqual(error.exception.code,'CONSENT_REQUIRED')
        self.assertEqual((root/'note.txt').read_bytes(),before)
        self.assertTrue(callable(getattr(plan,'consent',None)))
        # Native filesystem publication fault, not a fake SDK owner or ready ACK.
        from unittest.mock import patch
        original=engine.DefaultRecovery._publish
        calls=[]
        def fail_mid_publish(this,source,target):
            original(this,source,target)
            calls.append(str(target))
            if len(calls)==2: raise OSError('synthetic disk interruption')
        with patch.object(engine.DefaultRecovery,'_publish',fail_mid_publish):
            with self.assertRaises(engine.RecoveryError) as error: plan.apply(plan.consent())
        self.assertEqual(error.exception.code,'APPLY_ROLLED_BACK')
        self.assertTrue(callable(getattr(plan,'rollback',None)),'No durable rollback entry')
        self.assertEqual(plan.rollback(plan.consent())['state'],'rolled_back')
        self.assertEqual(plan.status()['state'],'rolled_back')
        self.assertEqual((root/'note.txt').read_bytes(),before)
        self.assertEqual((root/'installs/host').read_text(),'host-install')
        self.assertEqual((root/'profiles/sentinel/data').read_text(),'other-profile')
        stale=engine.prepare(cap,root,journal_root=journal,intent='replace-real-default')
        (root/'note.txt').write_text('changed after consent preview')
        with self.assertRaises(engine.RecoveryError) as changed:
            stale.apply(stale.consent())
        self.assertEqual(changed.exception.code,'TARGET_CHANGED_REPREPARE_REQUIRED')
        (root/'note.txt').write_bytes(before)
        # Mutation is after the final census, at the actual native delete boundary.
        real_delete=engine.DefaultRecovery._delete
        for mutation in ['hardlink', 'same-content-hardlink', 'same-content-inode', 'changed-bytes', 'symlink']:
            if mutation=='symlink':
                probe=scope/'symlink-capability-probe'
                try: probe.symlink_to(root/'note.txt')
                except OSError as exc:
                    print('SYMLINK_CONTROL_UNAVAILABLE',getattr(exc,'winerror',None))
                    continue
                else: probe.unlink()
            raced=engine.prepare(cap,root,journal_root=journal,intent='replace-real-default')
            leaf=root/'memories/old-only.md'
            other=scope/('unexpected-'+mutation)
            original_bytes=leaf.read_bytes()
            same_content=mutation in {'same-content-inode','same-content-hardlink'}
            other.write_bytes(original_bytes if same_content else b'unconsented replacement')
            fired=[]
            def inject(this,target):
                if target==leaf and not fired:
                    fired.append(True)
                    if mutation=='changed-bytes': target.write_bytes(b'unconsented replacement')
                    else:
                        target.unlink()
                        if mutation in {'hardlink','same-content-hardlink'}: os.link(other,target)
                        elif mutation=='symlink': target.symlink_to(other)
                        else: os.replace(other,target)
                return real_delete(this,target)
            with patch.object(engine.DefaultRecovery,'_delete',inject):
                with self.assertRaises(engine.RecoveryError, msg=mutation): raced.apply(raced.consent())
            self.assertTrue(fired,mutation)
            self.assertTrue(leaf.is_symlink() if mutation=='symlink' else leaf.exists(),mutation)
            self.assertEqual(leaf.read_bytes(),original_bytes if same_content else b'unconsented replacement',mutation)
            print('LEAF_SUBSTITUTION_REFUSED',mutation)
            # Reopen must also refuse to overwrite the unconsented generation.
            reopened_race=engine.DefaultRecovery(raced.directory,raced.plan_digest)
            with self.assertRaises(core.FinixError): reopened_race.rollback(raced.consent())
            leaf.unlink()
            # Restore authorized other entries only after fixture owner removes replacement.
            reopened_race.rollback(raced.consent())
        second=engine.prepare(cap,root,journal_root=journal,intent='replace-real-default')
        self.assertEqual(second.apply(second.consent())['state'],'published_offline')
        self.assertEqual((root/'note.txt').read_text(),'restored-note')
        self.assertFalse((root/'memories/old-only.md').exists(), 'stale default memory survived replacement')
        self.assertFalse((root/'skills/old-only/SKILL.md').exists(), 'stale active skill survived replacement')
        with closing(sqlite3.connect(root/'state.db')) as db:
            self.assertEqual(db.execute('select value from synthetic').fetchone(),('restored',))
            self.assertEqual(db.execute('pragma integrity_check').fetchone(),('ok',))
        self.assertEqual(second.apply(second.consent())['state'],'published_offline')
        reopened=engine.DefaultRecovery(second.directory,second.plan_digest)
        import json
        journal_file=second.directory/'journal.json'
        saved=journal_file.read_bytes()
        corrupt=json.loads(saved)
        corrupt['published'].append('../outside')
        corrupt['old']['../outside']=None
        core.atomic_json(journal_file,corrupt)
        with self.assertRaises(engine.RecoveryError): reopened.rollback(second.consent())
        self.assertEqual((root/'note.txt').read_text(),'restored-note')
        journal_file.write_bytes(saved)
        linked=scope/'linked-target'
        os.link(root/'note.txt',linked)
        with self.assertRaises(engine.RecoveryError): reopened.rollback(second.consent())
        self.assertEqual((root/'note.txt').read_text(),'restored-note')
        linked.unlink()
        self.assertEqual(reopened.rollback(second.consent())['state'],'rolled_back')
        self.assertEqual((root/'note.txt').read_bytes(),before)
        with closing(sqlite3.connect(root/'state.db')) as old_db:
            self.assertEqual(old_db.execute('select value from old_default').fetchone(),('old-root-data',))
            self.assertEqual(old_db.execute('pragma integrity_check').fetchone(),('ok',))
        self.assertEqual((root/'installs/host').read_text(),'host-install')
        third=engine.prepare(cap,root,journal_root=journal,intent='replace-real-default')
        synced=[]
        def check_durability(this,source,target):
            pending=engine._load(this.directory/'journal.json')['pending']
            self.assertEqual(pending,[target.relative_to(root).as_posix()])
            self.assertIn(this.directory,synced,'pending journal directory was not fsynced')
            original(this,source,target)
        with patch.object(engine,'_sync_directory',side_effect=lambda p:synced.append(p)), patch.object(engine.DefaultRecovery,'_publish',check_durability):
            third.apply(third.consent())
        third.rollback(third.consent())
        # A genuinely dead owned child leaves pending+replacement on disk.
        import subprocess,sys
        fourth=engine.prepare(cap,root,journal_root=journal,intent='replace-real-default')
        child='''import os,sys
from pathlib import Path
from hermes_constants import get_default_hermes_root,get_hermes_home
from hermes_cli.profiles import get_profile_dir
from finix.default_recovery import DefaultRecovery
root=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])/'sdk-root'
assert get_default_hermes_root()==root==get_profile_dir('default')
assert get_hermes_home()==root
p=DefaultRecovery(Path(sys.argv[1]),sys.argv[2])
original=DefaultRecovery._publish
calls=[]
def die(self,source,target):
    original(self,source,target)
    calls.append(1)
    if sys.argv[3]=='rollback' or len(calls)==2: os._exit(73)
DefaultRecovery._publish=die
getattr(p,sys.argv[3])(p.consent())
'''
        for action in ['apply','rollback']:
            result=subprocess.run([sys.executable,'-c',child,str(fourth.directory),fourth.plan_digest,action],timeout=30)
            self.assertEqual(result.returncode,73)
        durable=engine.DefaultRecovery(fourth.directory,fourth.plan_digest)
        self.assertEqual(durable.rollback(fourth.consent())['state'],'rolled_back')
        self.assertEqual(durable.rollback(fourth.consent())['state'],'rolled_back')
        self.assertEqual((root/'note.txt').read_bytes(),before)
        # Kill the owned native process at BOTH sides of a stale unlink. The
        # durable pending record must restore deletions without a completed ACK.
        deletion_child=child.replace("original=DefaultRecovery._publish", "original=DefaultRecovery._delete").replace("def die(self,source,target):", "def die(self,target):").replace("    original(self,source,target)", "    if sys.argv[3]=='before': os._exit(73)\n    original(self,target)\n    os._exit(73)").replace("DefaultRecovery._publish=die", "DefaultRecovery._delete=die").replace("getattr(p,sys.argv[3])(p.consent())", "p.apply(p.consent())")
        for boundary in ['before','after']:
            deleting=engine.prepare(cap,root,journal_root=journal,intent='replace-real-default')
            result=subprocess.run([sys.executable,'-c',deletion_child,str(deleting.directory),deleting.plan_digest,boundary],timeout=30)
            self.assertEqual(result.returncode,73)
            pending=engine._load(deleting.directory/'journal.json')['pending']
            self.assertTrue(pending[0].startswith(('memories/','skills/')))
            if boundary=='after':
                restoring=subprocess.run([sys.executable,'-c',child,str(deleting.directory),deleting.plan_digest,'rollback'],timeout=30)
                self.assertEqual(restoring.returncode,73)
            resumed=engine.DefaultRecovery(deleting.directory,deleting.plan_digest)
            self.assertEqual(resumed.rollback(resumed.consent())['state'],'rolled_back')
            self.assertEqual(resumed.rollback(resumed.consent())['state'],'rolled_back')
            self.assertEqual((root/'memories/old-only.md').read_text(),'old memory')
            self.assertEqual((root/'skills/old-only/SKILL.md').read_text(),'old skill')
        self.assertTrue((fourth.directory/'preimages/note.txt').exists())
        if os.name!='nt':
            # Death inside native capture, not merely before/after _delete.
            capturing=engine.prepare(cap,root,journal_root=journal,intent='replace-real-default')
            capture_child=child[:child.index('original=DefaultRecovery._publish')]+'''from finix import default_recovery as e
original=e._move_noreplace
def die(a,n,b,m):
    original(a,n,b,m)
    # Family capture now precedes ordinary stale deletion. Keep this control
    # targeted to the approved stale-leaf quarantine, not SQLite-family moves.
    if len(m)==64: os._exit(73)
e._move_noreplace=die
p.apply(p.consent())
'''
            result=subprocess.run([sys.executable,'-c',capture_child,str(capturing.directory),capturing.plan_digest,'apply'],timeout=30)
            self.assertEqual(result.returncode,73)
            captured=list((capturing.directory/'quarantine').iterdir())
            self.assertEqual(len(captured),1)
            # A new occupant blocks restoration; neither object is overwritten.
            occupied=root/'memories/old-only.md'
            occupied.write_bytes(b'new occupant after capture')
            reopening=engine.DefaultRecovery(capturing.directory,capturing.plan_digest)
            with self.assertRaises(core.FinixError): reopening.rollback(reopening.consent())
            self.assertEqual(occupied.read_bytes(),b'new occupant after capture')
            self.assertTrue(captured[0].exists())
            occupied.unlink()
            self.assertEqual(reopening.rollback(reopening.consent())['state'],'rolled_back')
            self.assertEqual(occupied.read_text(),'old memory')
            print('QUARANTINE_CRASH_REOPEN_AND_NONCLOBBER_OK')
        self.assertEqual((root/'tools/sentinel').read_text(),'protected tool')
        self.assertEqual((root/'plugins/finix/control').read_text(),'trusted control')
        self.assertEqual((root/'unknown-local-context').read_text(),'retained unknown')
        self.assertEqual((root/'memories/old-only.md').read_text(),'old memory')
        self.assertEqual((root/'skills/old-only/SKILL.md').read_text(),'old skill')
        self.assertEqual((root/'profiles/sentinel/data').read_text(),'other-profile')
        self.assertEqual((root/'installs/host').read_text(),'host-install')

if __name__ == '__main__': unittest.main()
