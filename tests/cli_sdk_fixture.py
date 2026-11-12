"""Offline modeled SDK transport; real Finix admission/import/history consumers.

This is not a live gateway/READY proof. Only kernel transport and authenticated
SDK RPC are doubled; native identities, pins, rosters and consent are not waived.
"""
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace


def host(local, monkeypatch, *, pending=False, entered=None, release=None, before_ack=None):
    from finix import core, recovery_shared_host as shared
    from gateway import host_rendezvous, status, owner_identity
    from hermes_cli import gateway_runtime_discovery as discovery
    from hermes_cli import gateway_runtime, gateway_client
    profile, scope = local
    root = core.private_dir(scope/'host')
    monkeypatch.setenv('HERMES_HOME',str(root))
    pid=os.getpid(); start=status.get_process_start_time(pid)
    roster=[{'home':str(p),'profile_id':str(p)} for p in (root,profile.home)]
    from hermes_constants import profile_name_for_home
    original_names={profile_name_for_home(p) for p in (root,profile.home)}
    calls=[]
    identity=dict(pid=pid,start_time=start,instance_id='synthetic-instance',
        authority_epoch='synthetic-epoch',hermes_home=str(root),state='ready',
        api_origin='http://127.0.0.1:1')
    def identify():return dict(identity,served_profiles=list(roster))
    def query(home,**kw):
        assert Path(home)==root
        return identify()
    def bound(home, expected, verb, params=None, *, before_write=None):
        assert Path(home)==root and expected['pid']==pid and expected['start_time']==start
        if verb=='identify':return identify()
        assert verb=='serve-profile' and set(params)=={'name'}
        if before_write:before_write()
        name=params['name']; target=root/'profiles'/name
        assert target.is_dir() and not (target/'gateway.parked').exists()
        calls.append(name)
        if entered is not None:entered.set()
        if release is not None:assert release.wait(4),'CLI must observe pending SDK boundary'
        if before_ack is not None:before_ack(target)
        if pending:return dict(pending=True,served=name,served_profiles=sorted(original_names|{name}))
        from hermes_state import SessionDB
        db=SessionDB(target/'state.db');db.close()
        roster.append({'home':str(target),'profile_id':str(target)})
        return dict(served=name,served_profiles=sorted(original_names|{name}))
    monkeypatch.setattr(host_rendezvous,'read_record',lambda *a:None)
    monkeypatch.setattr(discovery,'query_identify',query)
    monkeypatch.setattr(shared,'_bound',bound)
    monkeypatch.setattr(owner_identity,'host_owner_subject',lambda:'synthetic-local-owner')
    def endpoint(home):
        return SimpleNamespace(endpoint=SimpleNamespace(profile_id=str(home),
            instance_id=identity['instance_id'],api_origin=identity['api_origin'],home=Path(home)))
    monkeypatch.setattr(gateway_runtime,'discover_gateway_endpoint',endpoint)
    class RPC:
        def __init__(self, endpoint):self.target=endpoint.home
        async def rpc(self, method, **kw):
            from hermes_state import SessionDB
            db=SessionDB(self.target/'state.db')
            try:
                if method=='session.mutate':
                    assert kw['operation']=='import' and kw['expected_revision']==0
                    assert kw['payload']['sessions'][0]['user_id']=='synthetic-local-owner'
                    return db.import_sessions(kw['payload']['sessions'])
                assert method=='session.list' and kw=={'scope':'stored'}
                plan=json.loads((self.target/'.finix-paused/history-original/plan.json').read_text())
                return {'sessions':[{'session_id':s['id']} for s in plan['sessions']]}
            finally:db.close()
    @asynccontextmanager
    async def connect(endpoint):yield RPC(endpoint)
    monkeypatch.setattr(gateway_client,'connect_gateway',connect)
    def ticket(*a,**kw):return 'synthetic-ticket'
    monkeypatch.setattr(gateway_client,'_session_ticket',ticket)
    def urlopen(request,**kw):
        assert request.full_url.startswith(identity['api_origin']+'/api/sessions/')
        sid=request.full_url.split('/')[-2]
        target=root/'profiles'/calls[-1]
        from hermes_state import SessionDB
        db=SessionDB(target/'state.db')
        try:messages=db.export_session(sid)['messages']
        finally:db.close()
        return io.BytesIO(json.dumps({'messages':messages}).encode())
    monkeypatch.setattr('urllib.request.urlopen',urlopen)
    return calls
