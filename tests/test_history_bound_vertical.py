"""Synthetic offline authorization; no actual cloud, tokens, or user profiles."""
import hashlib, io, json, os, time
from pathlib import Path
from contextlib import closing
import sqlite3
import pytest
from test_recovery_history_ownership import fenced_root, seed


@pytest.mark.parametrize('variant',['new','old','mixed','foreign','tampered','manifest-field'])
def test_generated_age_service_handoff_sdk_import(tmp_path, monkeypatch, variant):
    from finix import core
    from finix.recovery import RecoveryService, CommittedSource
    from finix.recovery_import import RecoveryImporter
    from hermes_cli.profiles import get_profile_dir
    src=seed(tmp_path); home=src.parent
    (home/'config.yaml').write_text('model: {}\n')
    if variant=='mixed':
        with closing(sqlite3.connect(src)) as db:
            db.execute("update sessions set profile_name='foreign' where id='history-0'"); db.commit()
    if variant=='foreign':
        with closing(sqlite3.connect(src)) as db:
            db.execute("update sessions set profile_name='foreign'"); db.commit()
    if variant=='old':
        from finix import streaming
        monkeypatch.setattr(streaming,'_source_namespace',lambda fd:None)
    if variant=='manifest-field':
        from finix import streaming
        stamp_producer=streaming._profile_stamp
        monkeypatch.setattr(streaming,'_profile_stamp',lambda name,files:dict(stamp_producer(name,files),files_sha256='0'*64))
    before=src.read_bytes()
    key=tmp_path/'generated-key'; recipient=core.keygen(key)
    result=core.backup(home,tmp_path/'copies',tmp_path/'index',recipient,
        sqlite_temp_dir=core.private_dir(home/'cache/finix/sqlite-temp'))
    if variant=='manifest-field':
        monkeypatch.setattr(streaming,'_profile_stamp',stamp_producer)
    cipher=Path(result['path']); body=cipher.read_bytes()
    row=dict(id='synthetic-object',name=cipher.name,size=len(body),sha256=hashlib.sha256(body).hexdigest())
    ledger_source=None
    if variant=='old':
        # Actual production ledger producer; transport/account are declared synthetic.
        from finix.workspace_drive import WorkspaceDrive
        from test_workspace_reconcile import Transport, ACCOUNT_DIGEST
        from finix.recovery import committed_journal_source
        state=tmp_path/'synthetic-workspace-ledger'
        adapter=WorkspaceDrive(home,'a'*64,state,runner=Transport(),account_digest=ACCOUNT_DIGEST)
        committed=adapter.reconcile(cipher,completion=result)
        journal=json.loads((state/'workspace.json').read_text())
        assert journal['binding']['home_identity']==adapter._directory_identity(home)
        assert journal['records'][0]['sha256']==row['sha256']
        assert journal['records'][0]['size']==len(body)
        # Neither the upload binding nor setup receipt records capture-time native
        # source identity/namespace. It cannot be invented from current home/rows.
        assert 'captured_source_identity' not in journal['records'][0]
        assert 'recipient' not in journal['records'][0]
        ledger_source=committed_journal_source(connection='offline',generation='synthetic-ledger',
            journal=state/'workspace.json',expected_binding={k:journal[k] for k in ('profile_id','binding','account')},
            validate_record=lambda r:adapter.descriptor(r),download=lambda r,out,cancel:out.write(body))
    service=RecoveryService(profile_id='synthetic-backend',profile_root=home,activation='synthetic-epoch',protected_roots=[])
    try:
        service.add_source(ledger_source or CommittedSource('offline','synthetic-ledger',lambda:[dict(row)],lambda r,out,cancel:out.write(body)))
        entry=service.catalog('synthetic-backend','synthetic-epoch')[0]
        dest=service.allocate_destination('synthetic-backend','synthetic-epoch')
        job=service.start(profile_id='synthetic-backend',activation='synthetic-epoch',candidate_id=entry['id'],identity=io.BytesIO(key.read_bytes()),destination=dest)
        service._worker.join(30)
        if variant=='manifest-field':
            assert service.status(job)['status']=='failed'
            assert job not in service._handoffs
            assert not dest.path.exists()
            return
        assert service.status(job)['status']=='succeeded',service.status(job)
        handoff=service.handoff(job,'synthetic-backend','synthetic-epoch')
        if variant=='old':
            assert handoff['source_profile'] is None
            from finix.recovery_import import RecoveryImportError
            with pytest.raises(RecoveryImportError,match='HISTORY_SOURCE_BINDING_REQUIRED'):
                RecoveryImporter(service).import_job(job,profile_id='synthetic-backend',activation='synthetic-epoch',name='recovered',consent_import=True)
            assert not Path(get_profile_dir('recovered')).exists()
            assert src.read_bytes()==before
            return
        assert handoff['source_profile']['namespace']=='source'
        # Returned/editable copies are never authority.
        handoff['source_profile']['namespace']='foreign'
        assert service.handoff(job,'synthetic-backend','synthetic-epoch')['source_profile']['namespace']=='source'
        if variant=='tampered':
            with closing(sqlite3.connect(Path(handoff['profile_root'])/'state.db')) as db:
                db.execute("update messages set content='tampered'");db.commit()
        if variant in {'mixed','foreign','tampered'}:
            from finix.recovery_import import RecoveryImportError
            with pytest.raises(RecoveryImportError,match='HISTORY_OWNERSHIP_UNSUPPORTED|HISTORY_SOURCE_BINDING_REQUIRED'):
                RecoveryImporter(service).import_job(job,profile_id='synthetic-backend',activation='synthetic-epoch',name='recovered',consent_import=True)
            assert not Path(get_profile_dir('recovered')).exists()
            assert src.read_bytes()==before
            return
        imported=RecoveryImporter(service).import_job(job,profile_id='synthetic-backend',activation='synthetic-epoch',name='recovered',consent_import=True)
        assert imported['name']=='recovered'
        target=Path(get_profile_dir('recovered'))
        metadata=json.loads(next((home/'cache/finix/import').glob('*.json')).read_text())
        assert metadata['history_source_profile']['namespace']=='source'
        assert metadata['cipher_sha256']==row['sha256']
        with closing(sqlite3.connect(target/'state.db')) as db:
            assert db.execute('select count(*) from sessions where profile_name=\'recovered\'').fetchone()==(3,)
            assert db.execute('select count(*) from messages').fetchone()==(12,)
        assert src.read_bytes()==before
        if os.environ.get('FINIX_BOUND_NATIVE')=='1':
            from native_probe import probe
            probe(target,dict(source_sha256=hashlib.sha256(before).hexdigest(),
                source_bytes_unchanged=True,source_namespace='source',
                ciphertext_sha256=row['sha256'],ciphertext_size=row['size'],
                source_profile=metadata['history_source_profile'],
                delta=metadata['history_ownership']))
            assert src.read_bytes()==before
    finally: service.close()


from test_recovery_api import api


def test_request_fields_cannot_supply_source_namespace(api):
    client,module,profile,root=api
    (root/'synthetic.age').write_bytes(b'not an archive; no decrypt should occur')
    for field in ('source_profile','source_binding','history_payload','captured_source_identity'):
        answer=client.post('/recovery/prepare',json=dict(profile_id=profile.profile_id,
            archive_path=str(root/'synthetic.age'),destination_path=str(root/'destination'),
            consent_local=True,**{field:{'version':1,'namespace':'source'}}))
        assert answer.status_code==422,answer.text
    assert not (root/'destination').exists()

