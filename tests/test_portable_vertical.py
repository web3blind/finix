"""Real generated age -> service -> inert target -> native ticket WS."""
import hashlib, io, json, os, sqlite3
from contextlib import closing
from pathlib import Path
import pytest
from test_recovery_history_ownership import fenced_root, seed


@pytest.mark.parametrize('variant',['legacy-no-stamp','canonical','foreign-labels','canonical-lineage'])
def test_new_copy_real_native_vertical(tmp_path,monkeypatch,variant):
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
    if variant=='legacy-no-stamp':monkeypatch.setattr(streaming,'_source_namespace',lambda fd:None)
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
    (home/'config.yaml').write_text('model: {}\n')
    for rel in ['memories/note.md','skills/fixture/SKILL.md','tools/inert.txt','installs/inert.txt']:
        p=home/rel;core.private_dir(p.parent);p.write_text('fixture data')
    before=src.read_bytes()
    key=tmp_path/'generated-key';recipient=core.keygen(key)
    backup=core.backup(home,tmp_path/'copies',tmp_path/'index',recipient,sqlite_temp_dir=core.private_dir(home/'cache/finix/sqlite-temp'))
    cipher=Path(backup['path']).read_bytes()
    service=RecoveryService(profile_id='synthetic-backend',profile_root=home,activation='synthetic-epoch',protected_roots=[])
    try:
        row=dict(id='fixture',name=Path(backup['path']).name,size=len(cipher),sha256=hashlib.sha256(cipher).hexdigest())
        service.add_source(CommittedSource('offline','fixture',lambda:[row],lambda r,out,cancel:out.write(cipher)))
        selected=service.catalog('synthetic-backend','synthetic-epoch')[0]
        job=service.start(profile_id='synthetic-backend',activation='synthetic-epoch',candidate_id=selected['id'],identity=io.BytesIO(key.read_bytes()),destination=service.allocate_destination('synthetic-backend','synthetic-epoch'))
        service._worker.join(40);assert service.status(job)['status']=='succeeded',service.status(job)
        handoff=service.handoff(job,'synthetic-backend','synthetic-epoch')
        if variant=='legacy-no-stamp':assert handoff['source_profile'] is None
        receipt=RecoveryImporter(service).import_job(job,profile_id='synthetic-backend',activation='synthetic-epoch',name='recovered',consent_import=True,history_mode='new-copy')
        assert receipt['history_pending'] and not receipt['can_activate'] and not receipt['full_acceptance']
        target=Path(get_profile_dir('recovered'))
        original=target/'.finix-paused/history-original/state.db'
        assert core.digest_file(original)==handoff['new_copy_payload']['sha256']
        assert not (target/'state.db').exists()
        for rel in ['tools/inert.txt','installs/inert.txt']:
            assert (target/'.finix-paused/archive-files'/rel).read_text()=='fixture data'
        assert src.read_bytes()==before
        from native_probe import probe
        probe(target,dict(portable=handoff['new_copy_payload'],variant=variant,cipher_sha256=row['sha256']))
        assert core.digest_file(original)==handoff['new_copy_payload']['sha256']
        assert src.read_bytes()==before
        # SDK readback includes inactive storage and rich sidecars, separately
        # from the real native HTTP display projection exercised above.
        db=SessionDB(target/'state.db')
        try:
            plan=json.loads((target/'.finix-paused/history-original/plan.json').read_text())
            from gateway.session_contract import SessionActor,SessionRef
            from hermes_state_mutation_binding import authorize_history
            from gateway.session_mutations import _authorize_import
            from types import SimpleNamespace
            profile=str(target.resolve())
            with closing(sqlite3.connect(target/'state.db')) as c:
                c.row_factory=sqlite3.Row
                keys=[r[0] for r in c.execute('select key from state_meta')]
                assert any(k.startswith('gateway.history_control.v1.') for k in keys)
                assert not any(k.startswith('gateway.local_policy.v1:') for k in keys)
                from hermes_state_mutation_binding import history_binding
                from hermes_state_runtime import RuntimeStoreError
                profile,subject=json.loads(history_binding(c,plan['sessions'][0]['id']))
                assert authorize_history(c,SessionActor(subject,profile,frozenset({'session:read','session:create'}),'positive'),plan['sessions'][0]['id'])
                foreign=SessionActor('uid:999999',profile,frozenset({'session:read','session:create'}),'foreign')
                with pytest.raises(RuntimeStoreError,match='permission_denied'):authorize_history(c,foreign,plan['sessions'][0]['id'])
            for raw in plan['sessions']:
                imported=db.export_session(raw['id'],include_inactive=True)
                assert imported['parent_session_id']==raw['parent_session_id']
                assert imported['title_source']==raw['title_source']
                assert len(imported['messages'])==len(raw['messages'])
                for old,new in zip(raw['messages'],imported['messages']):
                    for field in ('content','message_uid','tool_calls','tool_call_id','tool_call_uids','reasoning','reasoning_content','reasoning_details','api_content','display_metadata','active','compacted'):
                        assert new.get(field)==old.get(field),(field,new.get(field),old.get(field))
            auth=SimpleNamespace(profile_id=profile,db=db)
            args={'payload':{'sessions':plan['sessions']}}
            for actor,reason in [(SessionActor(subject,'wrong',frozenset({'session:create'}),'wrong'),'profile_mismatch'),(SessionActor(subject,profile,frozenset({'session:read'}),'no-create'),'permission_denied')]:
                with pytest.raises(RuntimeStoreError,match=reason):_authorize_import(auth,actor,SessionRef(profile,plan['sessions'][0]['id']),args)
        finally:db.close()
        if variant=='legacy-no-stamp':
            followup=core.backup(target,tmp_path/'future-copies',tmp_path/'future-index',recipient,
                sqlite_temp_dir=core.private_dir(target/'cache/finix/sqlite-temp'))
            restored=tmp_path/'future-restored'
            core.restore(Path(followup['path']),key,restored)
            assert core.digest_file(restored/'.finix-paused/history-original/state.db')==handoff['new_copy_payload']['sha256']
    finally:service.close()
