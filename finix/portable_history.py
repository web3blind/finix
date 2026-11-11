"""Bounded NEW-content projection, never an archived-authority migration.

Preparation is inert. Completion requires the final target's genuine protected
native gateway connection; no SessionActor or policy receipt is constructed here.
Original DB/census/exports must be published durably by RecoveryImporter first.
"""
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from . import core

CONTRACT = 'sdk-new-content-copy-v1'


def _fail(code):
    from .recovery_import import RecoveryImportError
    raise RecoveryImportError(code)


def _json(path, value):
    import base64
    def encode(v):
        if isinstance(v, bytes): return {'sqlite_blob_base64':base64.b64encode(v).decode('ascii')}
        raise TypeError(type(v).__name__)
    core.atomic_json(path, json.loads(json.dumps(value, default=encode)))


def _census(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as c:
        if c.execute('pragma integrity_check').fetchone() != ('ok',):
            _fail('HISTORY_INVALID_CONTENT')
        tables=[r[0] for r in c.execute("select name from sqlite_master where type='table' order by name")]
        return {t:{'columns':[r[1] for r in c.execute('pragma table_info("'+t.replace('"','""')+'")')],
                   'rows':[list(r) for r in c.execute('select * from "'+t.replace('"','""')+'"')]} for t in tables}


def ordered(sessions):
    """Topological physical segments; missing parents/cycles/duplicate IDs refuse."""
    by_id={s['id']:s for s in sessions}
    if len(by_id)!=len(sessions): _fail('HISTORY_ID_COLLISION')
    output=[];done=set();pending=dict(by_id)
    while pending:
        ready=[sid for sid,s in pending.items() if not s.get('parent_session_id') or s['parent_session_id'] in done]
        if not ready: _fail('HISTORY_INVALID_LINEAGE')
        for sid in sorted(ready):
            output.append(pending.pop(sid));done.add(sid)
    return output


def prepare(source, stage, *, expected_payload):
    """Snapshot exact verified DB; public per-session export, no export_all cap."""
    stage=Path(stage);source=Path(source)
    original=stage/'original.db'
    with core.regular_input(source) as src, core.regular_output(original) as dst:
        import os
        with os.fdopen(os.dup(src),'rb') as a, os.fdopen(os.dup(dst),'wb') as b:shutil.copyfileobj(a,b)
    actual={'size':original.stat().st_size,'sha256':core.digest_file(original)}
    if expected_payload != actual: _fail('HISTORY_SOURCE_BINDING_REQUIRED')
    census=_census(original)
    _json(stage/'census.json',census)
    if 'sessions' not in census: _fail('HISTORY_INVALID_CONTENT')
    cols=census['sessions']['columns'];i=cols.index('id')
    ids=[r[i] for r in census['sessions']['rows']]
    if len(set(ids))!=len(ids):_fail('HISTORY_ID_COLLISION')
    # SDK may run storage migrations on this disposable COPY only, never original.
    working=stage/'export-working.db'
    with core.regular_input(original) as src, core.regular_output(working) as dst:
        import os
        with os.fdopen(os.dup(src),'rb') as a, os.fdopen(os.dup(dst),'wb') as b:shutil.copyfileobj(a,b)
    from hermes_state import SessionDB
    db=SessionDB(working)
    try:
        sessions=[db.export_session(sid,include_inactive=True) for sid in ids]
        if any(s is None for s in sessions):_fail('HISTORY_TRUNCATED')
        message_count=len(census.get('messages',{}).get('rows',[]))
        if sum(len(s['messages']) for s in sessions)!=message_count:_fail('HISTORY_TRUNCATED')
        sessions=ordered(sessions)
        uids=[m.get('message_uid') for s in sessions for m in s['messages'] if m.get('message_uid')]
        if len(uids)!=len(set(uids)):_fail('HISTORY_ID_COLLISION')
        # One session per batch permits arbitrarily many sessions, preserving
        # parent links via previously imported target-owned history bindings.
        for s in sessions:
            _,errors=db._validate_import_payload([s])
            if errors:_fail('HISTORY_SDK_BOUNDS')
        bounds={k:getattr(db,k) for k in ('_IMPORT_MAX_SESSIONS','_IMPORT_MAX_MESSAGES_PER_SESSION','_IMPORT_MAX_SESSION_BYTES','_IMPORT_MAX_TOTAL_BYTES','_IMPORT_MAX_TOTAL_MESSAGES')}
    finally:db.close()
    _json(stage/'exports.json',sessions)
    plan={'contract':CONTRACT,'original':actual,'sessions':sessions,'sdk_bounds':bounds,
          'logical_count':sum(not s.get('parent_session_id') for s in sessions),
          'full_acceptance':False,
          'projection_notice':{'owner_label':'new authenticated target owner; original labels retained in exports/census/raw DB',
              'policy':'no archived authority replay; later explicit user resume may issue NEW SDK policy',
              'omitted_session_columns':['profile_name','session_key','chat_id','chat_type','thread_id','origin_json','transport_profile','created_source','display_name','expiry_finalized','title_source','last_read_at','last_activity_at','last_activity_description'],
              'other_tables':'Only sessions/messages/resolved system prompts projected. Every table and unknown state_meta retained inert; no full-state acceptance.',
              'physical_message_ids':'new SQLite row IDs; durable message UIDs preserved'}}
    _json(stage/'plan.json',plan)
    if core.digest_file(original)!=actual['sha256']:_fail('HISTORY_SOURCE_BINDING_REQUIRED')
    return plan


async def _import_authenticated(rpc, target, *, owner_subject, expected_original, check=None):
    """Use an ALREADY AUTHENTICATED final-target stock gateway WS capability.

    Caller obtains rpc using SDK connect_gateway against a verified exact target.
    Never call on a staging-profile gateway: SDK bindings contain target identity.
    No implicit gateway start, actor synthesis, resume, model call or auth fallback.
    """
    target=Path(target);provenance=target/'.finix-paused/history-original'
    def fence():
        if check is not None:check()
    fence()
    if not isinstance(owner_subject,str) or not owner_subject:_fail('HISTORY_AUTH_REQUIRED')
    with core.locked(provenance):
        plan=json.loads((provenance/'plan.json').read_text(encoding='utf8'))
        if plan['contract']!=CONTRACT or plan['original']!=expected_original or core.digest_file(provenance/'state.db')!=expected_original['sha256']:
            _fail('HISTORY_SOURCE_BINDING_REQUIRED')
        sessions=ordered(plan['sessions'])
        # Public picker is not a complete physical inventory; inspect final new
        # store read-only before the first write, including hidden/inactive IDs.
        with closing(sqlite3.connect((target/'state.db').as_uri()+'?mode=ro',uri=True)) as c:
            if c.execute('select 1 from sessions limit 1').fetchone():_fail('HISTORY_ID_COLLISION')
        results=[]
        for n,raw in enumerate(sessions):
            new=copy.deepcopy(raw)
            new['user_id']=owner_subject
            new.pop('profile_name',None) # not an import-schema field; raw retained
            fence()
            answer=await rpc.rpc('session.mutate',session_id=new['id'],operation='import',
                request_id='finix-copy-'+expected_original['sha256']+'-'+str(n),
                expected_revision=0,payload={'sessions':[new]})
            fence()
            if not answer.get('ok') or answer.get('imported')!=1 or answer.get('skipped') or answer.get('detached') or answer.get('errors'):
                _fail('HISTORY_IMPORT_INCOMPLETE')
            results.append(answer)
        # These are newly authorized target-owned rows. Restore title provenance
        # through the SDK public mutator (never direct SQL writes).
        from hermes_state import SessionDB
        db=SessionDB(target/'state.db')
        differences={}
        try:
            for raw in sessions:
                if raw.get('title') is not None and raw.get('title_source'):
                    fence()
                    if not db.set_session_title_source(raw['id'],raw['title_source']):_fail('HISTORY_IMPORT_INCOMPLETE')
                    fence()
                observed=db.export_session(raw['id'],include_inactive=True)
                if observed is None or len(observed['messages'])!=len(raw['messages']):_fail('HISTORY_TRUNCATED')
                changed={k:{'archived':raw.get(k),'projection':observed.get(k)} for k in set(raw)|set(observed)
                    if k not in {'messages','timings'} and raw.get(k)!=observed.get(k)}
                rich=[]
                for a,b in zip(raw['messages'],observed['messages']):
                    delta={k:{'archived':a.get(k),'projection':b.get(k)} for k in set(a)|set(b) if a.get(k)!=b.get(k)}
                    if any(k not in {'id','session_id'} for k in delta):_fail('HISTORY_RICH_DATA_LOSS')
                    rich.append(delta)
                differences[raw['id']]={'session_fields':changed,'message_fields':rich}
        finally:db.close()
        receipt={'contract':CONTRACT,'original':expected_original,'results':results,'field_differences':differences,
                 'full_acceptance':False,'automatic_resume':False,'original_authority_replayed':False}
        fence()
        _json(provenance/'import-receipt.json',receipt)
        fence()
        return receipt


async def complete_target(target, *, name, expected_original, shared_runtime=None):
    """Attach ONLY to the final existing target's protected native owner.

    Does not launch a host or accept a caller-specified principal/capabilities.
    A lifecycle owner must separately authorize/verify scoped inert startup.
    """
    from hermes_constants import (get_default_hermes_root, set_hermes_home_override,
                                  reset_hermes_home_override)
    from hermes_cli.profiles import get_profile_dir, validate_profile_name
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_cli.gateway_runtime_discovery import query_identify
    from hermes_cli.gateway_client import connect_gateway
    from gateway.owner_identity import host_owner_subject
    validate_profile_name(name)
    target=core.checked_path(target)
    root=Path(get_default_hermes_root())
    resolved=Path(get_profile_dir(name))
    if name=='default' or not target.samefile(resolved) or not target.parent.samefile(root/'profiles'):
        _fail('HISTORY_TARGET_MISMATCH')
    if shared_runtime is None:
        identity=query_identify(target,timeout=2)
        if not identity or identity.get('state')!='ready' or not Path(identity['hermes_home']).samefile(target):
            _fail('HISTORY_TARGET_MISMATCH')
        roster=identity.get('served_profiles',[])
        if len(roster)!=1 or not Path(roster[0]['home']).samefile(target):_fail('HISTORY_TARGET_MISMATCH')
    else:
        from .recovery_shared_host import SharedTargetRuntime
        from .recovery_pipeline import _CAPS
        if (type(shared_runtime) is not SharedTargetRuntime or shared_runtime.record.get('target')!=target
                or shared_runtime.record.get('runtime') is not shared_runtime
                or not any(r is shared_runtime.record for r in _CAPS.values())):
            _fail('HISTORY_TARGET_MISMATCH')
        identity=shared_runtime.ready()
    endpoint=discover_gateway_endpoint(target).endpoint
    if endpoint is None:_fail('HISTORY_AUTH_REQUIRED')
    if shared_runtime is not None:shared_runtime.endpoint(endpoint)
    token=set_hermes_home_override(target)
    try:
        async with connect_gateway(endpoint) as rpc:
            # Native ticket producer derives real local subject from protected IPC.
            receipt=await _import_authenticated(rpc,target,owner_subject=host_owner_subject(),expected_original=expected_original,
                **({'check':shared_runtime.ready} if shared_runtime is not None else {}))
        after=shared_runtime.ready() if shared_runtime is not None else query_identify(target,timeout=2)
        if any(after.get(k)!=identity.get(k) for k in ('pid','instance_id','authority_epoch','hermes_home')):
            _fail('HISTORY_TARGET_MISMATCH')
        return receipt
    finally:reset_hermes_home_override(token)
