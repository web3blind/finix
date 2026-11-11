"""One generated offline-provider ordinary recovery pipeline; no real cloud."""
import asyncio,hashlib,io,json,os,sqlite3,subprocess,time
from contextlib import closing
from pathlib import Path
import pytest
from test_recovery_history_ownership import fenced_root,seed

@pytest.mark.parametrize('variant',['legacy-no-stamp','canonical','foreign-labels','canonical-lineage','blank-model','legacy-cli','durable-http','partial-history','root-multiplex'])
def test_ordinary_restore_pipeline(tmp_path,monkeypatch,variant,request):
    from finix import core, streaming
    from finix.recovery import RecoveryService,CommittedSource
    from finix.recovery_import import RecoveryImporter
    from hermes_cli.profiles import get_profile_dir
    from hermes_state import SessionDB
    if variant.startswith('canonical'):
        import asyncio
        from types import SimpleNamespace
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from gateway.session_authority import initialize_session_authority
        from gateway.session_local import create_local_session
        from gateway.session_contract import SessionActor
        from gateway.owner_identity import host_owner_subject
        from hermes_constants import set_hermes_home_override
        from gateway import run
        home=core.private_dir(Path(get_profile_dir('source')))
        set_hermes_home_override(home)
        monkeypatch.setattr(run,'_load_gateway_config',lambda *a,**k:{'platform_toolsets':{'gui':[]}})
        store=SessionStore(home/'sessions',GatewayConfig())
        authority=asyncio.run(initialize_session_authority(SimpleNamespace(session_store=store,_session_db=store._db,adapters={},_draining=False),profile_id=str(home),instance_id='fixture-source'))
        actor=SessionActor(host_owner_subject(),authority.profile_id,frozenset({'session:create','session:read'}),'fixture-source')
        for i in range(3):
            ref=create_local_session(authority,actor,{'request_id':'source-'+str(i),'source':'gui','model':'synthetic-no-provider','toolsets':[]})
            for j in range(4):store._db.append_message(ref.session_id,role='assistant',content='synthetic '+str(j),message_uid='canonical-'+str(i)+'-'+str(j),reasoning='reason',api_content='api sidecar',display_metadata={'fixture':True},codex_reasoning_items=[{'type':'compaction','encrypted_content':'synthetic-inert-checkpoint'}] if j==3 else None,tool_calls=[{'id':'call-'+str(i),'type':'function','function':{'name':'inert_fixture','arguments':'{}'}}] if j==0 else None)
        src=Path(store._db.db_path)
        assert store._db._conn.execute("select count(*) from state_meta where key like 'gateway.local_policy.v1:%'").fetchone()[0]==3
        store._db.close();set_hermes_home_override(None)
    else:
        src=seed(tmp_path);home=src.parent
    if variant=='root-multiplex':
        from hermes_constants import get_default_hermes_root
        import shutil
        home=Path(get_default_hermes_root())
        shutil.copyfile(src,home/'state.db');src=home/'state.db'
    if variant in ('legacy-no-stamp','legacy-cli'):monkeypatch.setattr(streaming,'_source_namespace',lambda fd:None,raising=False)
    if variant=='foreign-labels':
        with closing(sqlite3.connect(src)) as c:
            c.execute("update sessions set user_id='uid:999999',profile_name='foreign-source'");c.commit()
    if variant=='canonical-lineage':
        with closing(sqlite3.connect(src)) as c:
            ids=[r[0] for r in c.execute('select id from sessions order by id')]
            c.execute('update sessions set parent_session_id=? where id=?',(ids[0],ids[1]))
            c.execute("update sessions set end_reason='compression',ended_at=? where id=?",(1794300000,ids[0]));c.commit()
        db=SessionDB(src)
        mid=db.append_message(ids[2],role='assistant',content='inactive original',message_uid='inactive-preserved',api_content='inactive api sidecar')
        db.close()
        with closing(sqlite3.connect(src)) as c:
            c.execute('update messages set active=0,compacted=0 where id=?',(mid,));c.commit()
    # Public user-data producers, not fabricated authority. The projection
    # restores title provenance; exact model-attribution rows/read timestamps
    # are retained inert and explicitly prevent full-state acceptance.
    db=SessionDB(src)
    try:
        with closing(sqlite3.connect(src)) as c:all_ids=[r[0] for r in c.execute('select id from sessions order by id')]
        for n,sid in enumerate(all_ids):
            db.set_session_title(sid,'Archived title '+str(n))
            db.set_session_title_source(sid,db.TITLE_SOURCE_DERIVED)
    finally:db.close()
    (home/'config.yaml').write_text('model: {}\nplugins: {enabled: [fixture-trap]}\nhooks: {fixture: archived-trap}\n')
    trap=core.private_dir(home/'plugins/fixture-trap')
    (trap/'plugin.yaml').write_text('name: fixture-trap\nversion: 0.0.1\n')
    (trap/'__init__.py').write_text("from pathlib import Path\n(Path(__file__).parent/'trap-executed').write_text('EXECUTED')\nraise AssertionError('ARCHIVED_CODE_EXECUTED')\n")
    for rel in ['memories/note.md','skills/fixture/SKILL.md','tools/inert.txt','installs/inert.txt']:
        p=home/rel;core.private_dir(p.parent);p.write_text('fixture data')
    root_runtime=None
    if variant=='root-multiplex':
        from root_multiplex_fixture import start_root
        root_runtime=start_root(home,request)
    before=src.read_bytes()
    from finix.integration import Profile
    from finix.recovery_http import create_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_constants import get_default_hermes_root
    producer=(Path(__file__).parents[1]/'web/keys.mjs').as_uri()
    script="import {createKeySession} from "+json.dumps(producer)+";const s=createKeySession();const recipient=await s.generate();process.stdout.write(JSON.stringify({recipient,text:s.downloadText()}));"
    pair=json.loads(subprocess.run(['node','--input-type=module','-e',script],capture_output=True,check=True).stdout)
    assert '\u2014' in pair['text']
    profile=Profile(home,default_root=Path(get_default_hermes_root()))
    backup=core.backup(home,profile.root/'copies',profile.root/'index',pair['recipient'],sqlite_temp_dir=core.private_dir(home/'cache/finix/sqlite-temp'))
    cipher=Path(backup['path']).read_bytes()
    if variant=='legacy-cli':
        from finix import plugin_cli
        import argparse,contextlib
        from hermes_constants import set_hermes_home_override,reset_hermes_home_override
        parser=argparse.ArgumentParser();plugin_cli.setup_parser(parser)
        token=set_hermes_home_override(home)
        key=tmp_path/'generated-export.txt';key.write_text(pair['text'],encoding='utf8')
        original_target=tmp_path/'archive-verified';core.restore(Path(backup['path']),key,original_target)
        original_digest=core.digest_file(original_target/'state.db')
        try:
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                assert plugin_cli.run(parser.parse_args(['recovery','list']),profile_factory=lambda:profile)==0
            selected=json.loads(output.getvalue())['items'][0]
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                code=plugin_cli.run(parser.parse_args(['recovery','restore','--copy-id',selected['id'],'--identity',str(key)]),profile_factory=lambda:profile)
            rows=[json.loads(line) for line in output.getvalue().splitlines()]
            assert code==0,rows
            receipt=rows[-1]
            assert receipt['backend_target_ready'] and receipt['content_verified'] and not receipt['full_acceptance']
            assert receipt['history']==dict(stored_logical=3,displayed_messages=12,physical_messages=12)
            target=Path(get_profile_dir(receipt['name']))
            assert core.digest_file(target/'.finix-paused/history-original/state.db')==original_digest
            assert src.read_bytes()==before
            return
        finally:reset_hermes_home_override(token)
    def authenticated_source():
        if variant in ('durable-http','partial-history','root-multiplex'):
            from hermes_constants import set_hermes_home_override
            set_hermes_home_override(home)
        return profile
    router=create_router(authenticated_source);adapter=router.recovery_adapter
    service=adapter.service(profile)
    row=dict(id='fixture',name=Path(backup['path']).name,size=len(cipher),sha256=hashlib.sha256(cipher).hexdigest(),complete=True,omitted_count=0)
    service.add_source(CommittedSource('offline','fixture',lambda:[row],lambda r,out,cancel:out.write(cipher)))
    app=FastAPI();app.include_router(router)
    with TestClient(app,client=('127.0.0.1',51000)) as client:
        if root_runtime is not None:
            capabilities=client.get('/recovery/capabilities')
            assert capabilities.status_code==200,capabilities.text
            assert capabilities.json()['bootstrap_condition']==dict(contract='cooperative-root-boundaries-v1',condition='no-concurrent-same-user-target-replacement',atomic_target_binding=False)
        items=client.get('/recovery/catalog').json()['items'];selected=next(x for x in items if x['sha256']==row['sha256'])
        if variant in ('durable-http','partial-history','root-multiplex'):
            import uuid,threading
            from finix.recovery_operations import RecoveryOperations
            from hermes_constants import get_hermes_home
            actual_execute=RecoveryOperations._execute
            captured=[]
            def dispatch_context(self,worker_service,owned):
                assert Path(get_hermes_home()).samefile(home)
                captured.append(True)
                return actual_execute(self,worker_service,owned)
            monkeypatch.setattr(RecoveryOperations,'_execute',dispatch_context)
            if variant=='partial-history':
                import finix.portable_history as portable
                real_authenticated=portable._import_authenticated
                committed=[]
                async def interrupted_history(rpc,*a,**kw):
                    class LostAck:
                        async def rpc(self,*args,**kwargs):
                            answer=await rpc.rpc(*args,**kwargs)
                            if args[0]=='session.mutate':
                                committed.append(answer)
                                if len(committed)==2:raise ConnectionError('synthetic lost batch ACK')
                            return answer
                    try:
                        return await real_authenticated(LostAck(),*a,**kw)
                    except ConnectionError:
                        # The same genuine target-issued native capability is
                        # still live. Existing helper refuses its own two exact
                        # committed batches; retain this negative, never erase.
                        from finix.recovery_import import RecoveryImportError
                        with pytest.raises(RecoveryImportError) as refusal:
                            await real_authenticated(rpc,*a,**kw)
                        assert refusal.value.code=='HISTORY_ID_COLLISION'
                        raise
                monkeypatch.setattr(portable,'_import_authenticated',interrupted_history)
            import finix.recovery_pipeline as pipeline
            real_import=RecoveryImporter.import_job
            if root_runtime is not None:
                real_bootstrap=pipeline._bootstrap
                def observed_bootstrap(cap):
                    try:return real_bootstrap(cap)
                    except BaseException:
                        import traceback
                        traceback.print_exc()
                        raise
                monkeypatch.setattr(pipeline,'_bootstrap',observed_bootstrap)
            entered=threading.Event();release=threading.Event();imports=[]
            def blocked_import(self,*a,**kw):
                imports.append(True);entered.set()
                assert release.wait(15)
                return real_import(self,*a,**kw)
            monkeypatch.setattr(RecoveryImporter,'import_job',blocked_import)
            intent=dict(profile_id=profile.profile_id,candidate_id=selected['id'],consent_restore=True,request_id=str(uuid.uuid4()))
            ack=client.post('/recovery/operations',json=intent)
            assert ack.status_code==200,ack.text
            job=ack.json();url='/recovery/operations/'+job['id']
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                status=client.get(url).json()
                if status['phase']=='awaiting_key':break
                time.sleep(.02)
            assert status['phase']=='awaiting_key',status
            name=status['name'];phase_id=str(uuid.uuid4())
            started=time.monotonic()
            key_ack=client.post(url+'/key?request_id='+phase_id,files={'file':('recovery.txt',pair['text'].encode('utf8'),'text/plain')})
            assert key_ack.status_code==200,key_ack.text
            assert time.monotonic()-started<3,'key request waited for backend pipeline'
            try:
                assert entered.wait(60 if root_runtime is not None else 10)
                started=time.monotonic();status=client.get('/recovery/operations/current').json()
                assert time.monotonic()-started<1 and status['phase']=='importing'
                assert not status['cancellable']
                assert client.post(url+'/cancel').status_code==409
                assert client.post('/recovery/operations',json=intent).json()['id']==job['id']
                replay=dict(intent,request_id=str(uuid.uuid4()))
                assert client.post('/recovery/operations',json=replay).status_code==409
                # Legacy mutation cannot acquire an independent owner.
                assert client.post('/recovery/prepare-auto',json={k:v for k,v in intent.items() if k!='request_id'}).status_code==409
                # Genuine registered CLI replay sees this exact HTTP-owned operation;
                # it must not catalog, read an identity or acquire another worker.
                from finix import plugin_cli
                import argparse,contextlib
                from hermes_constants import set_hermes_home_override,reset_hermes_home_override
                parser=argparse.ArgumentParser();plugin_cli.setup_parser(parser)
                token=set_hermes_home_override(home)
                try:
                    for request_id,expected_code in [(intent['request_id'],0),(str(uuid.uuid4()),1)]:
                        output=io.StringIO()
                        with contextlib.redirect_stdout(output):
                            result=plugin_cli.run(parser.parse_args(['recovery','restore','--copy-id',selected['id'],'--identity',str(tmp_path/'MUST-NOT-READ'),'--request-id',request_id]),profile_factory=lambda:profile)
                        lines=[json.loads(line) for line in output.getvalue().splitlines()]
                        assert result==expected_code,lines
                        if expected_code==0:
                            assert lines[-1]['id']==job['id'] and lines[-1]['state']=='running' and lines[-1]['phase']=='importing'
                            assert lines[-1]['activated'] is False
                        else:assert lines[-1]['error']=='BUSY',lines
                finally:reset_hermes_home_override(token)
                assert imports==[True]
            finally:release.set()
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                status=client.get(url).json()
                if status['state']!='running':break
                time.sleep(.05)
            if variant=='partial-history':
                assert status['state']=='interrupted' and status.get('receipt') is None,status
                assert len(committed)==2 and all(x.get('imported')==1 for x in committed)
                target=Path(get_profile_dir(name))
                with closing(sqlite3.connect(target/'state.db')) as db:
                    assert db.execute('select count(*) from sessions').fetchone()[0]==2
                    assert db.execute('select count(*) from messages').fetchone()[0]==8
                preserved=core.digest_file(target/'.finix-paused/history-original/state.db')
                from finix.recovery_operations import RecoveryOperations
                assert RecoveryOperations().current(service)['state']=='interrupted'
                assert client.post('/recovery/operations',json=dict(intent,request_id=str(uuid.uuid4()))).status_code==409
                assert client.post('/recovery/operations',json=intent).json()['state']=='interrupted'
                assert core.digest_file(target/'.finix-paused/history-original/state.db')==preserved
                assert src.read_bytes()==before
                service.close()
                return
            assert status['state']=='succeeded',status
            receipt=status['receipt']
            core.atomic_json(tmp_path/'actual-operation-public.json',status)
            assert receipt['backend_target_ready'] and receipt['history']==dict(stored_logical=3,displayed_messages=12,physical_messages=12)
            assert imports==[True]
            assert client.post('/recovery/operations',json=intent).json()['receipt']==receipt
            target=Path(get_profile_dir(name))
            if root_runtime is not None:
                import finix.recovery_pipeline as pipeline
                assert receipt['bootstrap_contract']==dict(version='cooperative-root-boundaries-v1',condition='no-concurrent-same-user-target-replacement',atomic_target_binding=False,checked_after_cleanup=True)
                assert not pipeline._OWNED_TARGETS
                root_runtime.verify(target)
            assert src.read_bytes()==before
            key=tmp_path/'fixture-key';key.write_text(pair['text'],encoding='utf8')
            future=core.backup(target,tmp_path/'future-copies',tmp_path/'future-index',pair['recipient'],sqlite_temp_dir=core.private_dir(target/'cache/finix/sqlite-temp'))
            restored=tmp_path/'future-restored';core.restore(Path(future['path']),key,restored)
            assert core.digest_file(restored/'.finix-paused/history-original/state.db')==core.digest_file(target/'.finix-paused/history-original/state.db')
            service.close()
            return
        prepared=client.post('/recovery/prepare-auto',json=dict(profile_id=profile.profile_id,candidate_id=selected['id'],consent_restore=True))
        assert prepared.status_code==200,prepared.text
        job=prepared.json();url='/recovery/jobs/'+job['id'];name=job['name']
        key_ack=client.post(url+'/key',files={'file':('recovery.txt',pair['text'].encode('utf8'),'text/plain')})
        assert key_ack.status_code==200,key_ack.text
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            status=client.get(url).json()
            if status['state']!='running':break
            time.sleep(.02)
        assert status['state']=='succeeded',status
        assert status['archive_verified'] is True
        assert status['backend_target_ready'] is False and status['full_acceptance'] is False
        verified_original=service.handoff(adapter.jobs[job['id']]['service_id'],profile.profile_id,service.activation)['new_copy_payload']['sha256']
        import finix.recovery_pipeline as pipeline
        real_complete=pipeline.complete_restore
        def observed_complete(*a,**kw):
            try:return real_complete(*a,**kw)
            except BaseException:
                import traceback
                traceback.print_exc()
                raise
        monkeypatch.setattr(pipeline,'complete_restore',observed_complete)
        if variant=='legacy-no-stamp':
            real_bootstrap=pipeline._bootstrap
            def guarded_bootstrap(cap):
                record=pipeline._CAPS[cap]
                foreign=core.private_dir(tmp_path/'foreign-target')
                changes={'target':foreign,'root_identity':('wrong',),'activation':'wrong-epoch',
                    'purpose':'public-bypass','digest':{},'original':{'size':0,'sha256':'0'*64}}
                for field,bad in changes.items():
                    old=record[field];record[field]=bad
                    try:
                        with pytest.raises(Exception):real_bootstrap(cap)
                        assert not pipeline._OWNED_TARGETS,'negative must refuse BEFORE startup'
                    finally:record[field]=old
                return real_bootstrap(cap)
            monkeypatch.setattr(pipeline,'_bootstrap',guarded_bootstrap)
        imported=client.post(url+'/import-auto',json=dict(profile_id=profile.profile_id,consent_restore=True))
        assert imported.status_code==200,imported.text
        receipt=imported.json()
        # This assertion is the product seam: pending/False must not escape as success.
        assert receipt.get('backend_target_ready') is True,receipt
        assert receipt.get('content_verified') is True and receipt.get('archive_verified') is True
        assert receipt.get('full_acceptance') is False
        target=Path(get_profile_dir(name))
        if root_runtime is not None:
            import finix.recovery_pipeline as pipeline
            assert not pipeline._OWNED_TARGETS, 'shared root must never be an owned child'
            root_runtime.verify(target)
        assert core.digest_file(target/'.finix-paused/history-original/state.db')==verified_original
        assert src.read_bytes()==before
        assert not adapter.jobs[job['id']]['destination'].path.exists()
        assert receipt['history']['stored_logical']==(2 if variant=='canonical-lineage' else 3)
        assert receipt['history']['displayed_messages']==12
        assert receipt['history']['physical_messages']==(13 if variant=='canonical-lineage' else 12)
        assert (target/'.finix-paused/plugins/fixture-trap/__init__.py').is_file()
        assert not list(home.rglob('trap-executed')) and not list(target.rglob('trap-executed'))
        key=tmp_path/'fixture-key';key.write_text(pair['text'],encoding='utf8')
        future=core.backup(target,tmp_path/'future-copies',tmp_path/'future-index',pair['recipient'],sqlite_temp_dir=core.private_dir(target/'cache/finix/sqlite-temp'))
        restored=tmp_path/'future-restored';core.restore(Path(future['path']),key,restored)
        assert core.digest_file(restored/'.finix-paused/history-original/state.db')==verified_original
    service.close()
