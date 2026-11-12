"""Bounded NEW-content projection, never an archived-authority migration.

Preparation is inert. Completion requires the final target's genuine protected
native gateway connection; no SessionActor or policy receipt is constructed here.
Original DB/census/exports must be published durably by RecoveryImporter first.
"""
from contextlib import closing
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
from . import core

CONTRACT = 'sdk-new-content-copy-v1'
# Finix's per-session RPC admission budget (UTF-8 JSON, excluding derived
# timings). No archive-wide session/message count cap: each physical session
# is submitted separately. These are Finix policy, not private SDK constants
# or a promise that a future server accepts the same payload.
MAX_SESSION_BYTES = 5 * 1024 * 1024
MAX_SESSION_MESSAGES = 10_000
SESSION_TEXT_FIELDS = ('parent_session_id', 'source', 'user_id', 'model', 'system_prompt',
    'end_reason', 'cwd', 'git_branch', 'git_repo_root', 'billing_provider',
    'billing_base_url', 'billing_mode', 'cost_status', 'cost_source', 'pricing_version', 'title')
MESSAGE_TEXT_FIELDS = ('tool_call_id', 'tool_name', 'effect_disposition', 'finish_reason',
    'reasoning', 'reasoning_content', 'platform_message_id', 'message_id', 'message_uid')
SESSION_INTEGER_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens',
    'cache_write_tokens', 'reasoning_tokens', 'api_call_count')
SESSION_NUMBER_FIELDS = ('estimated_cost_usd', 'actual_cost_usd')


def _validate_fields(row, *, text=(), integers=(), numbers=(), epochs=()):
    for key in text:
        if row.get(key) is not None and not isinstance(row[key], str):
            _fail('HISTORY_INVALID_CONTENT')
    for key in integers:
        value = row.get(key)
        if value is not None and (type(value) is not int or not -(1 << 63) <= value < (1 << 63)):
            _fail('HISTORY_INVALID_CONTENT')
    for key in (*numbers, *epochs):
        value = row.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            _fail('HISTORY_INVALID_CONTENT')
        # Cross-platform epoch window accepted by the public history transport.
        if key in epochs and value is not None and not 0 <= value <= 4_200_000_000:
            _fail('HISTORY_INVALID_CONTENT')


def validate_sessions(sessions):
    """Validate without normalizing IDs, truncating content, or SDK internals."""
    if not isinstance(sessions, list):
        _fail('HISTORY_INVALID_CONTENT')
    message_ids, message_uids = set(), set()
    for session in sessions:
        if (not isinstance(session, dict) or not isinstance(session.get('id'), str)
                or not session['id'] or session['id'] != session['id'].strip()
                or not isinstance(session.get('messages'), list)
                or any(not isinstance(m, dict) for m in session['messages'])):
            _fail('HISTORY_INVALID_CONTENT')
        try:
            size = len(json.dumps({k:v for k,v in session.items() if k != 'timings'},
                ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            _fail('HISTORY_INVALID_CONTENT')
        if size > MAX_SESSION_BYTES or len(session['messages']) > MAX_SESSION_MESSAGES:
            _fail('HISTORY_CONTENT_BOUNDS')
        _validate_fields(session, text=SESSION_TEXT_FIELDS, integers=SESSION_INTEGER_FIELDS,
                         numbers=SESSION_NUMBER_FIELDS, epochs=('started_at','ended_at'))
        config = session.get('model_config')
        if config is not None:
            try:
                parsed = json.loads(config) if isinstance(config, str) else config
                if not isinstance(parsed, dict): _fail('HISTORY_INVALID_CONTENT')
                json.dumps(parsed, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                _fail('HISTORY_INVALID_CONTENT')
        for message in session['messages']:
            if not isinstance(message.get('role'), str) or not message['role']:
                _fail('HISTORY_INVALID_CONTENT')
            _validate_fields(message, text=MESSAGE_TEXT_FIELDS, integers=('token_count','id'),
                             epochs=('timestamp',))
            if message.get('session_id') != session['id']:
                _fail('HISTORY_INVALID_LINEAGE')
            mid, uid = message.get('id'), message.get('message_uid')
            if mid is None: _fail('HISTORY_INVALID_CONTENT')
            if mid in message_ids or (uid and uid in message_uids):
                _fail('HISTORY_ID_COLLISION')
            message_ids.add(mid)
            if uid: message_uids.add(uid)
    return ordered(sessions)


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


def _verify_inventory(census, sessions):
    """Match physical identities, not only totals (which can hide substitutions)."""
    inventories = {}
    for table, required in (('sessions', {'id','parent_session_id','title'}),
                            ('messages', {'id','session_id'})):
        data = census.get(table)
        if not isinstance(data, dict) or not required <= set(data.get('columns', [])):
            _fail('HISTORY_INVALID_CONTENT')
        rows = [dict(zip(data['columns'], row)) for row in data['rows']]
        by_id = {row['id']: row for row in rows}
        if len(by_id) != len(rows): _fail('HISTORY_ID_COLLISION')
        inventories[table] = by_id
    original_sessions, original_messages = inventories['sessions'], inventories['messages']
    if set(original_sessions) != {s['id'] for s in sessions}: _fail('HISTORY_TRUNCATED')
    messages = [m for s in sessions for m in s['messages']]
    if set(original_messages) != {m['id'] for m in messages} or len(messages) != len(original_messages):
        _fail('HISTORY_TRUNCATED')
    for session in sessions:
        original = original_sessions[session['id']]
        if any(original.get(k) != session.get(k) for k in ('parent_session_id','title','title_source')):
            _fail('HISTORY_RICH_DATA_LOSS')
    for message in messages:
        original = original_messages[message['id']]
        if original['session_id'] != message['session_id']:
            _fail('HISTORY_INVALID_LINEAGE')
        if original.get('message_uid') != message.get('message_uid'):
            _fail('HISTORY_ID_COLLISION')


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
    for table, required in (('sessions', {'id','parent_session_id','title'}),
                            ('messages', {'id','session_id','role','content'})):
        if table not in census or not required <= set(census[table]['columns']):
            _fail('HISTORY_INVALID_CONTENT')
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
        sessions=validate_sessions(sessions)
        _verify_inventory(census, sessions)
    finally:db.close()
    _json(stage/'exports.json',sessions)
    plan={'contract':CONTRACT,'original':actual,'sessions':sessions,
          'validation':{'owner':'finix','version':1,'max_session_bytes':MAX_SESSION_BYTES,
                        'max_session_messages':MAX_SESSION_MESSAGES,'batch_sessions':1},
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
        sessions=validate_sessions(plan['sessions'])
        # The persisted plan is not authority for its own completeness. Rebuild
        # from a newly hash-bound raw copy using the public per-session exporter
        # and physical ID census, including inactive rows. SDK migrations touch
        # only the disposable copy; the preserved original stays byte-identical.
        import tempfile
        with tempfile.TemporaryDirectory(dir=core.private_dir(provenance/'validation'),
                                         prefix='inventory-') as scratch:
            trusted=prepare(provenance/'state.db',core.private_dir(Path(scratch)),
                            expected_payload=expected_original)
            _verify_inventory(_census(Path(scratch)/'original.db'),sessions)
            canonical=lambda rows:json.dumps(rows,sort_keys=True,ensure_ascii=False,
                                             separators=(',', ':'),allow_nan=False)
            if canonical(sessions)!=canonical(trusted['sessions']):
                _fail('HISTORY_RICH_DATA_LOSS')
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
                if any(observed.get(k) != raw.get(k) for k in ('id','parent_session_id','title')):
                    _fail('HISTORY_RICH_DATA_LOSS')
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
