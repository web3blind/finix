"""Real disk/SDK/age transaction regressions, no lifecycle ACK substitutes."""
import os
import sqlite3
import unittest
import uuid
import time
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from finix import core, default_recovery as engine, sqlite_family as family


class FamilyTests(unittest.TestCase):
    def setUp(self):
        from hermes_constants import get_default_hermes_root,get_hermes_home
        from hermes_cli.profiles import get_profile_dir
        self.root=get_default_hermes_root()
        self.scope=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        self.assertEqual(self.root,self.scope/'sdk-root')
        self.assertEqual(self.root,get_hermes_home())
        self.assertEqual(self.root,get_profile_dir('default'))
        core.private_dir(self.root)
        self.directory=core.private_dir(self.root/'cache/finix/family-tests'/uuid.uuid4().hex)

    def test_foreign_wal_is_not_a_runtime_derived_auxiliary(self):
        path=self.directory/'state.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table fixture(value)');db.commit()
        Path(str(path)+'-wal').write_bytes(b'foreign-wal-not-sqlite')
        with self.assertRaises(core.FinixError):
            engine._sqlite_generation(path,core.private_dir(self.directory/'snapshots'))

    def test_absent_main_is_sealed_and_rollback_restores_absence(self):
        self.addCleanup(self.clean_family)
        cap=self.capability();plan=self.prepare(cap)
        p=plan._plan()
        self.assertIsNone(p['sqlite_families']['state.db']['generation'])
        self.assertIn('runtime',p['sqlite_families']['state.db']['capture_pins'])
        self.assertEqual(plan.apply(plan.consent())['state'],'published_offline')
        self.assertEqual(plan.rollback(plan.consent())['state'],'rolled_back')
        self.assertFalse(any(os.path.lexists(self.root/name) for name in family.NAMES))

    def capability(self):
        from finix.recovery import RecoveryService,CommittedSource
        source=core.private_dir(self.directory/'source')
        (source/'config.yaml').write_text('{}\n')
        with closing(sqlite3.connect(source/'state.db')) as db:
            db.execute('create table incoming(value)')
            db.execute("insert into incoming values ('incoming')");db.commit()
        key=self.directory/'generated.key'; recipient=core.keygen(key)
        out,index=self.directory/'cipher',self.directory/'index'
        core.backup(source,out,index,recipient,sqlite_temp_dir=core.private_dir(source/'cache/finix/sqlite'))
        service=RecoveryService(profile_id='default',profile_root=self.root,activation='family-fixture',protected_roots=[])
        self.addCleanup(service.close)
        row=core.load_state(index)['backups'][0];cipher=out/row['name']
        row=dict(row,size=cipher.stat().st_size)
        def transfer(record,output,cancel):
            with cipher.open('rb') as stream:
                while chunk:=stream.read(65536): output.write(chunk)
        service.add_source(CommittedSource('synthetic-local','fixture',lambda:[row],transfer))
        selected=service.catalog('default','family-fixture')[0]
        job=service.start(selected['id'],identity=key,destination=service.allocate_destination('default','family-fixture'),profile_id='default',activation='family-fixture')
        deadline=time.monotonic()+30
        while service.status(job)['status']=='running' and time.monotonic()<deadline: time.sleep(.02)
        self.assertEqual(service.status(job)['status'],'succeeded',service.status(job))
        return engine.verified_import(service,job,profile_id='default',activation='family-fixture',name='recovered-family')

    def old_database(self):
        path=self.root/'state.db'
        self.addCleanup(self.clean_family)
        with closing(sqlite3.connect(path)) as db:
            db.execute('pragma journal_mode=WAL')
            db.execute('create table previous(value)')
            db.execute("insert into previous values ('old committed row')");db.commit()

    def clean_family(self):
        for name in family.NAMES:
            path=self.root/name
            if os.path.lexists(path): path.unlink()

    def prepare(self,cap):
        return engine.prepare(cap,self.root,journal_root=self.root/'cache/finix/default-recovery',intent='replace-real-default')

    def check_old(self):
        with closing(sqlite3.connect(self.root/'state.db')) as db:
            self.assertEqual(db.execute('select value from previous').fetchone(),('old committed row',))
            self.assertEqual(db.execute('pragma integrity_check').fetchone(),('ok',))

    def test_post_binding_same_byte_auxiliary_substitution_is_preserved(self):
        self.old_database();cap=self.capability();plan=self.prepare(cap)
        real=family.publish
        replacement=self.directory/'foreign'
        replaced=[]
        def substitute(this,p,j):
            leaf=self.root/'state.db-wal'
            replacement.write_bytes(leaf.read_bytes())
            os.replace(replacement,leaf)
            replaced.append(engine._leaf(leaf))
            return real(this,p,j)
        with patch.object(family,'publish',substitute),self.assertRaises(engine.RecoveryError):
            plan.apply(plan.consent())
        self.assertEqual(engine._leaf(self.root/'state.db-wal'),replaced[0])
        reopened=engine.DefaultRecovery(plan.directory,plan.plan_digest)
        with self.assertRaises(core.FinixError): reopened.rollback(reopened.consent())
        self.assertEqual(engine._leaf(self.root/'state.db-wal'),replaced[0])

    def test_real_committed_row_schema_and_main_identity_changes_refuse_apply(self):
        self.old_database();cap=self.capability()
        for statement in ("insert into previous values ('later')",'create table changed_schema(value)','pragma user_version=91'):
            plan=self.prepare(cap)
            with closing(sqlite3.connect(self.root/'state.db')) as db:
                db.execute(statement);db.commit()
            with self.assertRaises(engine.RecoveryError) as caught: plan.apply(plan.consent())
            self.assertEqual(caught.exception.code,'TARGET_CHANGED_REPREPARE_REQUIRED')
        plan=self.prepare(cap)
        replacement=self.directory/'same-bytes-main'
        replacement.write_bytes((self.root/'state.db').read_bytes())
        os.replace(replacement,self.root/'state.db')
        with self.assertRaises(engine.RecoveryError): plan.apply(plan.consent())

    def test_published_reentry_rejects_new_auxiliary_without_overwriting(self):
        self.old_database();cap=self.capability();plan=self.prepare(cap)
        plan.apply(plan.consent())
        wal=self.root/'state.db-wal';wal.write_bytes(b'foreign-generation')
        unexpected=engine._leaf(wal)
        with self.assertRaises(engine.RecoveryError): plan.apply(plan.consent())
        self.assertEqual(engine._leaf(wal),unexpected)
        with self.assertRaises(engine.RecoveryError): plan.rollback(plan.consent())
        self.assertEqual(engine._leaf(wal),unexpected)

    def test_commit_between_snapshot_and_physical_binding_refuses(self):
        self.old_database();cap=self.capability();plan=self.prepare(cap)
        real=engine._verify_sqlite_generation;fired=[]
        def commit_after_snapshot(path,temp,sealed):
            result=real(path,temp,sealed)
            if not fired:
                fired.append(True)
                with closing(sqlite3.connect(path)) as db:
                    db.execute("insert into previous values ('unconsented committed row')");db.commit()
            return result
        with patch.object(engine,'_verify_sqlite_generation',commit_after_snapshot):
            with self.assertRaises(engine.RecoveryError): plan.apply(plan.consent())
        self.assertTrue(fired)
        with closing(sqlite3.connect(self.root/'state.db')) as db:
            self.assertEqual(db.execute('select count(*) from previous').fetchone(),(2,))

    def test_death_during_private_preimage_copy_can_retry_without_target_mutation(self):
        import subprocess,sys
        self.old_database();cap=self.capability();plan=self.prepare(cap)
        before=engine._identity(self.root/'state.db')
        child='''import os,sys
from pathlib import Path
from hermes_constants import get_default_hermes_root,get_hermes_home
from hermes_cli.profiles import get_profile_dir
root=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])/'sdk-root'
assert get_default_hermes_root()==root==get_profile_dir('default')
assert get_hermes_home()==root
from finix import default_recovery as e
original=e._copy
def die(source,target):
    if target.parent.name=='preimages':
        with e.core.regular_output(target) as fd: os.write(fd,b'partial-copy')
        os._exit(73)
    original(source,target)
e._copy=die
p=e.DefaultRecovery(Path(sys.argv[1]),sys.argv[2])
p.apply(p.consent())
'''
        result=subprocess.run([sys.executable,'-c',child,str(plan.directory),plan.plan_digest],timeout=30)
        self.assertEqual(result.returncode,73)
        self.assertEqual(engine._identity(self.root/'state.db'),before)
        self.assertEqual(plan.status()['state'],'prepared')
        reopened=engine.DefaultRecovery(plan.directory,plan.plan_digest)
        self.assertEqual(reopened.apply(reopened.consent())['state'],'published_offline')
        reopened.rollback(reopened.consent());self.check_old()

    def test_auxiliary_links_and_unexpected_names_refuse_before_snapshot(self):
        self.old_database();temp=core.private_dir(self.directory/'snapshots')
        for name in ('state.db-journal','state.db-unexpected'):
            path=self.root/name;path.write_bytes(b'')
            with self.assertRaises(core.FinixError): engine._sqlite_generation(self.root/'state.db',temp)
            path.unlink()
        sealed=engine._sqlite_generation(self.root/'state.db',temp)
        wal=self.root/'state.db-wal';alias=self.directory/'hardlink'
        os.link(wal,alias)
        with self.assertRaises(core.FinixError): engine._verify_sqlite_generation(self.root/'state.db',temp,sealed)
        alias.unlink()
        saved=self.directory/'original-wal';os.replace(wal,saved)
        try: wal.symlink_to(saved)
        except OSError as exc:
            print('SYMLINK_CONTROL_UNAVAILABLE',getattr(exc,'winerror',None))
        else:
            with self.assertRaises(core.FinixError): engine._verify_sqlite_generation(self.root/'state.db',temp,sealed)
            wal.unlink()
        os.replace(saved,wal)

    def test_family_child_death_boundaries_reopen_idempotently(self):
        import subprocess,sys
        self.old_database();cap=self.capability()
        child='''import os,sys
from pathlib import Path
from hermes_constants import get_default_hermes_root,get_hermes_home
from hermes_cli.profiles import get_profile_dir
root=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])/'sdk-root'
assert get_default_hermes_root()==root==get_profile_dir('default')
assert get_hermes_home()==root
from finix import sqlite_family as family
from finix.default_recovery import DefaultRecovery
p=DefaultRecovery(Path(sys.argv[1]),sys.argv[2])
original=family._move
def die(engine,source,target,expected):
    label=('capture:'+source.name if source.parent==root else
           'restore:'+source.name if source.parent.name=='sqlite-quarantine' else 'publish')
    if label==sys.argv[4] and sys.argv[5]=='before': os._exit(73)
    original(engine,source,target,expected)
    if label==sys.argv[4] and sys.argv[5]=='after': os._exit(73)
family._move=die
original_delete=DefaultRecovery._delete
def die_delete(this,target):
    if sys.argv[4]=='remove-incoming' and sys.argv[5]=='before': os._exit(73)
    original_delete(this,target)
    if sys.argv[4]=='remove-incoming' and sys.argv[5]=='after': os._exit(73)
DefaultRecovery._delete=die_delete
getattr(p,sys.argv[3])(p.consent())
'''
        for action,labels in [('apply',['capture:state.db-wal','capture:state.db-shm','capture:state.db','publish']),
                              ('rollback',['remove-incoming','restore:state.db','restore:state.db-wal','restore:state.db-shm'])]:
            for label in labels:
                for boundary in ('before','after'):
                    plan=self.prepare(cap)
                    if action=='rollback': plan.apply(plan.consent())
                    result=subprocess.run([sys.executable,'-c',child,str(plan.directory),plan.plan_digest,action,label,boundary],timeout=30)
                    self.assertEqual(result.returncode,73,(action,label,boundary))
                    reopened=engine.DefaultRecovery(plan.directory,plan.plan_digest)
                    self.assertEqual(reopened.rollback(reopened.consent())['state'],'rolled_back')
                    self.assertEqual(reopened.rollback(reopened.consent())['state'],'rolled_back')
                    self.check_old()
                    print('FAMILY_CHILD_DEATH_REOPEN',action,label,boundary,flush=True)

    def test_committed_wal_physical_family_is_quarantined_and_restored(self):
        import subprocess,sys
        self.addCleanup(self.clean_family)
        child='''import os
from pathlib import Path
from hermes_constants import get_default_hermes_root,get_hermes_home
from hermes_cli.profiles import get_profile_dir
root=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])/'sdk-root'
assert get_default_hermes_root()==root==get_profile_dir('default')
assert get_hermes_home()==root
import sqlite3
db=sqlite3.connect(root/'state.db')
db.execute('pragma journal_mode=WAL')
db.execute('pragma wal_autocheckpoint=0')
db.execute('create table previous(value)')
db.execute("insert into previous values ('old committed row')")
db.commit()
os._exit(73)
'''
        result=subprocess.run([sys.executable,'-c',child],timeout=30)
        self.assertEqual(result.returncode,73)
        self.assertGreater((self.root/'state.db-wal').stat().st_size,32)
        cap=self.capability();plan=self.prepare(cap)
        real=family._move;seen=[]
        def observe(this,source,target,leaf):
            if source.parent.name=='sqlite-staging':
                self.assertFalse((self.root/'state.db').exists())
                self.assertFalse((self.root/'state.db-wal').exists())
                self.assertFalse((self.root/'state.db-shm').exists())
                seen.append(True)
            return real(this,source,target,leaf)
        with patch.object(family,'_move',observe): plan.apply(plan.consent())
        self.assertEqual(seen,[True])
        j=engine._load(plan.directory/'journal.json')
        manifest=j['sqlite']['manifest']['members']
        self.assertGreater(manifest['state.db-wal']['size'],32)
        for name,leaf in manifest.items():
            if leaf: self.assertEqual(engine._leaf(plan.directory/'sqlite-quarantine'/name),leaf)
        reopened=engine.DefaultRecovery(plan.directory,plan.plan_digest)
        reopened.rollback(reopened.consent())
        for name,leaf in manifest.items():
            self.assertEqual(engine._leaf(self.root/name) if (self.root/name).exists() else None,leaf)
        self.check_old()
