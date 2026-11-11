"""RED integration tracer: genuine age/RecoveryService, native committed WAL.

Run only with the adjacent owned-root runner. No fabricated handoff/SDK drain.
"""
import os
import time
import sqlite3
import unittest
from pathlib import Path
from contextlib import closing
from finix import core, default_recovery as engine


class SQLiteGenerationTests(unittest.TestCase):
    def test_checkpoint_only_generation_can_publish_and_rollback(self):
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.profiles import get_profile_dir
        from finix.recovery import RecoveryService, CommittedSource
        from finix import streaming
        scope = Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        root = get_default_hermes_root()
        self.assertEqual(root, scope/'sdk-root')
        self.assertEqual(root, get_profile_dir('default'))
        self.assertEqual(root, get_hermes_home())
        core.private_dir(root)
        source = core.private_dir(scope/'source')
        (source/'config.yaml').write_text('{}\n')
        with closing(sqlite3.connect(source/'state.db')) as connection:
            connection.execute('create table incoming(value text)')
            connection.execute("insert into incoming values ('restored')")
            connection.commit()
        # Keep committed WAL live; closing this connection would checkpoint it.
        with closing(sqlite3.connect(root/'state.db')) as old:
            old.execute('pragma journal_mode=WAL')
            old.execute('pragma wal_autocheckpoint=0')
            old.execute('create table previous(value text)')
            old.execute("insert into previous values ('committed-before-consent')")
            old.commit()
            key=scope/'generated.key'
            recipient=core.keygen(key)
            out,index=scope/'cipher',scope/'index'
            core.backup(source,out,index,recipient,sqlite_temp_dir=core.private_dir(source/'cache/finix/sqlite'))
            service=RecoveryService(profile_id='default',profile_root=root,
                activation='sqlite-generation-fixture',protected_roots=[])
            self.addCleanup(service.close)
            row=core.load_state(index)['backups'][0]
            cipher=out/row['name']
            row=dict(row,size=cipher.stat().st_size)
            def transfer(record,output,cancel):
                with cipher.open('rb') as stream:
                    while chunk:=stream.read(65536): output.write(chunk)
            service.add_source(CommittedSource('synthetic-local','fixture',lambda:[row],transfer))
            selected=service.catalog('default','sqlite-generation-fixture')[0]
            job=service.start(selected['id'],identity=key,
                destination=service.allocate_destination('default','sqlite-generation-fixture'),
                profile_id='default',activation='sqlite-generation-fixture')
            deadline=time.monotonic()+30
            while service.status(job)['status']=='running' and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertEqual(service.status(job)['status'],'succeeded',service.status(job))
            cap=engine.verified_import(service,job,profile_id='default',
                activation='sqlite-generation-fixture',name='recovered-sqlite')
            plan=engine.prepare(cap,root,journal_root=root/'cache/finix/default-recovery',
                intent='replace-real-default')
            temp=core.private_dir(root/'cache/finix/sqlite-generation')
            def disk_snapshot():
                import hashlib
                if os.name=='nt':
                    from finix.windows_native import directory
                    parent=directory(root)
                else:
                    from contextlib import contextmanager
                    @contextmanager
                    def parent_fd():
                        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                        try: yield fd
                        finally: os.close(fd)
                    parent=parent_fd()
                with parent as anchor,core.regular_input(root/'state.db') as fd:
                    with streaming.sqlite_payload(fd,anchor,'state.db',temp,None,None) as (stream,size):
                        digest=hashlib.sha256()
                        while chunk:=stream.read(1024*1024): digest.update(chunk)
                        return digest.hexdigest()
            before=core.digest_file(root/'state.db')
            snapshot_before=disk_snapshot()
            self.assertEqual(old.execute('pragma wal_checkpoint(FULL)').fetchone()[0],0)
            after=core.digest_file(root/'state.db')
            self.assertNotEqual(before,after,'fixture did not perform a physical checkpoint')
            self.assertEqual(snapshot_before,disk_snapshot(),'checkpoint changed committed snapshot')
            print('ACTUAL_CHECKPOINT_RAW',before,after)
            print('GUARDED_DISK_SNAPSHOT',snapshot_before)
            # No simulated drain or ready acknowledgement: this is a cold-engine
            # tracer only; genuine supported-drain acceptance remains separate.
            old.close()
            self.assertEqual(plan.apply(plan.consent())['state'],'published_offline')
            reopened=engine.DefaultRecovery(plan.directory,plan.plan_digest)
            self.assertEqual(reopened.rollback(reopened.consent())['state'],'rolled_back')
            with closing(sqlite3.connect(root/'state.db')) as restored:
                self.assertEqual(restored.execute('select value from previous').fetchone(),
                    ('committed-before-consent',))
                self.assertEqual(restored.execute('pragma integrity_check').fetchone(),('ok',))
