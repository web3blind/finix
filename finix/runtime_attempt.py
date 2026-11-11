"""Pre-success failed-start DATA custody. Never interpret/open mixed SQLite.

Caller admission is the real SDK default role, existing native trust and writer
fence. An execution manifest authorizes DATA preservation, never old preimages.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import weakref
from . import core, default_recovery as cold, sqlite_family as family

POLICY='runtime-attempt-preserve-v1'
_NOTICE=('Failed pre-success startup may briefly write profile data. Preserve all '
         'allowed current variants durably before restoring OLD. Custody is included '
         'in future backups and retained until separately authorized cleanup.')
_PROOFS=weakref.WeakKeyDictionary()
class _Stopped: pass

def token(name):
    return hashlib.sha256(name.encode('utf-8')).hexdigest()

def _copy_blob(engine,source,target, *, logical=None, decode=False):
    # Partial copies stay in the EXISTING excluded transaction cache. The full
    # source is still retained; only complete verified copies enter custody.
    import uuid
    directory=core.private_dir(engine.directory/'custody-copy-staging')
    temporary=directory/uuid.uuid4().hex
    before=family._admitted_leaf(source)
    if logical is None and not decode:
        cold._copy(source,temporary)
        expected = {k: before[k] for k in ('size', 'sha256')}
    else:
        from . import custody_payload as container
        with core.regular_input(source) as fd, core.regular_output(temporary) as out:
            # Buffered fd wrapper writes all bytes, leaves the retained fd owned.
            with os.fdopen(os.dup(out), 'wb') as stream:
                if decode:
                    expected, _ = container.decode(fd, stream)
                else:
                    expected = container.encode(fd, logical, stream)
                stream.flush(); os.fsync(stream.fileno())
        cold._sync_directory(temporary.parent)
    staged=family._admitted_leaf(temporary)
    if (family._admitted_leaf(source)!=before
        or any(staged[k]!=expected[k] for k in ('size','sha256'))):
        raise cold.RecoveryError('TARGET_CHANGED')
    if logical is not None:
        _container(temporary, logical, expected)
    family._move(engine,temporary,target,staged)


def _container(path, logical, stored):
    from .custody_payload import decode, _private
    before=family._admitted_leaf(path)
    with core.regular_input(path) as fd:
        try:
            _private(fd)
            observed, representation = decode(fd)
            _private(fd)
        except ValueError: raise cold.RecoveryError('PREIMAGE_CHANGED') from None
    if family._admitted_leaf(path)!=before: raise cold.RecoveryError('PREIMAGE_CHANGED')
    if observed != {k:logical[k] for k in ('size','sha256')} or representation != stored:
        raise cold.RecoveryError('PREIMAGE_CHANGED')


def _pin():
    return family._admitted_leaf(Path(__file__))['sha256']

def policy(root,job,p):
    names=sorted(set(p['entries'])|set(p['deletions'])|(set(family.NAMES) if p['sqlite_families'] else set()))
    return dict(policy=POLICY,storage_format='opaque-container-v2',custody=str(root/'recovery-custody'/job),names=names,
        helper_sha256=_pin(),archive_helper_sha256=family._admitted_leaf(Path(__file__).with_name('custody_payload.py'))['sha256'],
        consent_notice=_NOTICE,retention='explicit-cleanup-only',
        incoming_custody={n:d for n,d in p['source_digest'].items() if n.startswith('recovery-custody/')},
        storage_cost='complete old copies + complete current copies and native originals',
        keepSharedResources=True)

def preserve_incoming(engine,p):
    """Flat archive DATA merge, not profile/shared resource replacement.

    Existing whole namespaces dedupe only after exact native-safe inventory;
    mismatches refuse without overwriting. Own partial merges can resume.
    """
    incoming=p['runtime_attempt']['incoming_custody']
    if not incoming:return
    root=Path(p['root']);source=Path(p['source']);record=engine.directory/'incoming-custody.json'
    with core.locked(source) as source_root:
        # Validate complete v2 namespaces using the normal DATA producer. No v1
        # metadata migration and no recursive wrapping of incoming containers.
        discovered=core.scan(source_root)
        observed={n:fp.opaque['sha256'] for n,fp in discovered.items() if n.startswith('recovery-custody/')}
        if observed!=incoming: raise cold.RecoveryError('SOURCE_CHANGED')
    groups={}
    for n,digest in incoming.items():
        parts=n.split('/')
        if (len(parts) not in (3,4) or not cold._hex(parts[1],32)
            or parts[1]==p['id'] or 'recovery-custody' in parts[1:]
            or not core.safe_name(n) or not cold._hex(digest)):
            raise cold.RecoveryError('CUSTODY_NAMESPACE_CONFLICT')
        groups.setdefault(parts[1],{})['/'.join(parts[2:])]=digest
    prior=cold._load(record) if record.exists() else None
    if prior is not None and (prior.get('plan_digest')!=engine.plan_digest or prior.get('incoming')!=incoming):
        raise cold.RecoveryError('INVALID_JOURNAL')
    with ExitStack() as guards:
        # Check every existing namespace before the first copy/publication.
        for namespace,expected in groups.items():
            target=root/'recovery-custody'/namespace
            if target.exists():
                cold._retain(guards,target,root)
                with ExitStack() as census:
                    actual={n:d for n,(_,d) in cold._inventory(target,census).items()}
                if (actual!=expected if prior is None else any(n not in expected or expected[n]!=d for n,d in actual.items())):
                    raise cold.RecoveryError('CUSTODY_NAMESPACE_CONFLICT')
        state=dict(version=1,plan_digest=engine.plan_digest,incoming=incoming,pending=None)
        cold._write(record,state)
        for n,digest in incoming.items():
            state['pending']=n;cold._write(record,state)
            target=root/n;original=source/n
            expected=family._admitted_leaf(original)
            if expected['sha256']!=digest:raise cold.RecoveryError('SOURCE_CHANGED')
            core.private_dir(target.parent);cold._retain(guards,target.parent,root)
            if target.exists():
                observed=family._admitted_leaf(target)
                if (observed['sha256'],observed['size'])!=(digest,expected['size']):
                    raise cold.RecoveryError('CUSTODY_NAMESPACE_CONFLICT')
            else:_copy_blob(engine,original,target)
            if family._admitted_leaf(original)!=expected or family._admitted_leaf(target)['sha256']!=digest:
                raise cold.RecoveryError('SOURCE_CHANGED')
        state['pending']=None;cold._write(record,state)

def validate_policy(p):
    if p['runtime_attempt']!=policy(Path(p['root']),p['id'],p):
        raise cold.RecoveryError('PINNED_PACKAGE_CHANGED')

def stopped_attempt(engine,runtime):
    # Not serializable process authority. Cleanup must have completed against the
    # retained Popen/native handles; a durable PID is never sufficient.
    if (not getattr(runtime,'_stopped',False)
        or getattr(runtime,'attempt_plan_digest',None)!=engine.plan_digest
        or getattr(runtime,'_ready_observed',False)
        or (runtime.child is not None and runtime.child.poll() is None)
        or (runtime.child is None and not getattr(runtime,'_never_spawned',False))
        or (os.name=='nt' and runtime.child is not None
            and (runtime.native is None or not runtime.native.runtime_pids))
        or cold._identity(runtime.root)!=engine._plan()['root_identity']):
        raise cold.RecoveryError('ATTEMPT_STOP_UNVERIFIED')
    proof=_Stopped();_PROOFS[proof]=engine.plan_digest
    return proof

def require_stopped_attempt(engine,proof):
    from gateway.runtime_ownership import live_runtime_owner
    if live_runtime_owner(Path(engine._plan()['root'])) is not None:
        raise cold.RecoveryError('REOPEN_LIVE_OWNER_UNVERIFIED')
    if proof not in _PROOFS or _PROOFS[proof]!=engine.plan_digest:
        raise cold.RecoveryError('ATTEMPT_STOP_UNVERIFIED')

def commit_success(engine,identity):
    """Fresh owned READY is already observed; errors MUST NOT roll back."""
    value=dict(version=1,plan_digest=engine.plan_digest,identity=identity)
    _immutable(engine,engine.directory/'ready-observed.json',value)
    _immutable(engine,engine.directory/'success-commit.json',value)

def _immutable(engine,path,value):
    # Unique partial leaf and NoClobber final publication, like family preimages.
    import uuid
    tmp=path.parent/('.manifest-'+uuid.uuid4().hex)
    encoded=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    if len(encoded)>cold.MAX_RECORD: raise cold.RecoveryError('JOURNAL_TOO_LARGE')
    with core.regular_output(tmp) as fd:
        view=memoryview(encoded)
        while view:
            n=os.write(fd,view);view=view[n:]
        os.fsync(fd)
    cold._sync_directory(path.parent)
    family._move(engine,tmp,path,cold._leaf(tmp))

def _leaf(path):
    return family._admitted_leaf(path) if os.path.lexists(path) else None

def _unknown(root,names):
    for child in root.iterdir():
        if ((child.name.casefold()=='state.db' or child.name.casefold().startswith('state.db-'))
            and child.name not in family.NAMES):
            raise cold.RecoveryError('UNKNOWN_RUNTIME_LEAF')
    # A new unknown installed skill/memory must not be left active after OLD restart.
    with ExitStack() as stack:
        if set(cold._owned_inventory(root,stack))-set(names):
            raise cold.RecoveryError('UNKNOWN_RUNTIME_LEAF')

def _manifest(engine,p,j,base):
    root=Path(p['root']); names=p['runtime_attempt']['names']
    _unknown(root,names)
    current={n:_leaf(root/n) for n in names}
    if j['sqlite']:
        incoming=j['sqlite']['manifest']['incoming']
        if current['state.db'] is None or current['state.db']['identity']!=incoming['identity']:
            raise cold.RecoveryError('TARGET_CHANGED')
    parents={str((root/n).parent):cold._identity((root/n).parent) for n in names}
    old={n:None for n in names}
    for n,d in j['old'].items():
        if d is not None:
            leaf=_leaf(engine.directory/'preimages'/n)
            if leaf is None or leaf['sha256']!=d: raise cold.RecoveryError('PREIMAGE_CHANGED')
            old[n]=leaf
    if j['sqlite']:
        for n,leaf in j['sqlite']['manifest']['members'].items():
            if leaf:
                if _leaf(engine.directory/'sqlite-quarantine'/n)!=leaf:
                    raise cold.RecoveryError('PREIMAGE_CHANGED')
                old[n]=leaf
    from . import custody_payload as container
    stored={}
    for kind, leaves in [('old',old),('copies',current),('objects',current)]:
        for n, leaf in leaves.items():
            if leaf is None: continue
            source=root/n
            if kind=='old':
                source=(engine.directory/'sqlite-quarantine'/n if n in family.NAMES and j['sqlite'] else engine.directory/'preimages'/n)
            with core.regular_input(source) as fd:
                stored[kind+'/'+token(n)]=container.encode(fd, leaf)
            if _leaf(source)!=leaf: raise cold.RecoveryError('TARGET_CHANGED')
    result=dict(version=2,policy=POLICY,storage_format=container.STORAGE_FORMAT,namespace=p['id'],
        stored=stored,plan_digest=engine.plan_digest,root_identity=p['root_identity'],parents=parents,current=current,old=old)
    container.validate_manifest(result)
    _immutable(engine,base/'execution-manifest.json',result)
    return result

def rollback(engine,consent,proof):
    p=engine._plan()
    if p['version']!=4: raise cold.RecoveryError('RUNTIME_ATTEMPT_NOT_AUTHORIZED')
    if consent!=engine.consent(): raise cold.RecoveryError('CONSENT_REQUIRED')
    if (engine.directory/'success-commit.json').exists() or (engine.directory/'ready-observed.json').exists():
        raise cold.RecoveryError('SUCCESS_RECONCILIATION_REQUIRED')
    root=cold._root(p['root']); base=Path(p['runtime_attempt']['custody'])
    from gateway.runtime_ownership import exclusive_maintenance,live_runtime_owner
    from .backup_jobs import file_lease
    from .default_recovery_lifecycle import writer_fence
    # Proof required for first census. Durable manifest permits only DATA
    # continuation after crash, under absent-owner + exclusive SDK reservation.
    if not (base/'execution-manifest.json').exists(): require_stopped_attempt(engine,proof)
    if live_runtime_owner(root) is not None: raise cold.RecoveryError('REOPEN_LIVE_OWNER_UNVERIFIED')
    with file_lease(engine.directory/'worker.lock'),writer_fence(root),exclusive_maintenance([root],purpose='finix-runtime-attempt-preserve'),ExitStack() as guards:
        guards.enter_context(core.locked(root));engine._fence(p,guards)
        j=engine._journal(p)
        for d in [base,base/'copies',base/'old',base/'objects',base/'restore-staging',engine.directory/'custody-originals']:
            core.private_dir(d);cold._retain(guards,d,root);cold._private(d)
        manifest_file=base/'execution-manifest.json'
        m=cold._load(manifest_file) if manifest_file.exists() else _manifest(engine,p,j,base)
        from . import custody_payload as container
        try: container.validate_manifest(m)
        except (ValueError, TypeError, KeyError): raise cold.RecoveryError('INVALID_JOURNAL') from None
        if (m.get('version')!=2 or m.get('namespace')!=p['id'] or m.get('policy')!=POLICY or m.get('plan_digest')!=engine.plan_digest
            or m.get('root_identity')!=p['root_identity'] or cold._identity(root)!=p['root_identity']
            or set(m.get('current',{}))!=set(p['runtime_attempt']['names'])
            or set(m.get('old',{}))!=set(p['runtime_attempt']['names'])):
            raise cold.RecoveryError('INVALID_JOURNAL')
        for parent,identity in m['parents'].items():
            if cold._identity(Path(parent))!=identity: raise cold.RecoveryError('TARGET_CHANGED')
        progress_file=base/'progress.json'
        progress=cold._load(progress_file) if progress_file.exists() else dict(version=2,manifest_digest=cold._digest(m),state='capturing',pending=None,restore={})
        if progress.get('manifest_digest')!=cold._digest(m): raise cold.RecoveryError('INVALID_JOURNAL')
        def pending(action):
            progress['pending']=action;cold._write(progress_file,progress)
        def check_scope():
            _unknown(root,m['current'])
            for n,leaf in m['current'].items():
                saved=_leaf(engine.directory/'custody-originals'/token(n));actual=_leaf(root/n)
                restored=progress['restore'].get(n)
                if saved is not None and saved!=leaf: raise cold.RecoveryError('TARGET_CHANGED')
                if saved is None:
                    allowed=[leaf] if progress['state']=='capturing' else [None,restored]
                else: allowed=[None,restored]
                if actual not in allowed: raise cold.RecoveryError('TARGET_CHANGED')
        check_scope()
        # ALL bytes are copied before ANY canonical move. Reopening never treats
        # a partially copied final basename as authority.
        for kind,leaves in [('old',m['old']),('copies',m['current'])]:
            for n,leaf in leaves.items():
                if leaf is None: continue
                target=base/kind/token(n)
                if target.exists():
                    _container(target,leaf,m['stored'][kind+'/'+token(n)])
                    continue
                if progress['state']!='capturing': raise cold.RecoveryError('PREIMAGE_CHANGED')
                source=(engine.directory/'preimages'/n if kind=='old' else root/n)
                if kind=='old' and n in family.NAMES and j['sqlite']:
                    source=engine.directory/'sqlite-quarantine'/n
                if _leaf(source)!=leaf: raise cold.RecoveryError('TARGET_CHANGED')
                pending('copy:'+kind+':'+n)
                _copy_blob(engine,source,target,logical=leaf)
                _container(target,leaf,m['stored'][kind+'/'+token(n)])
                if _leaf(source)!=leaf:
                    raise cold.RecoveryError('TARGET_CHANGED')
        for n,leaf in m['current'].items():
            if leaf is None: continue
            saved_container=base/'objects'/token(n)
            if not saved_container.exists():
                if progress['state']!='capturing': raise cold.RecoveryError('PREIMAGE_CHANGED')
                pending('container:'+n)
                _copy_blob(engine,base/'copies'/token(n),saved_container)
            _container(saved_container,leaf,m['stored']['objects/'+token(n)])
        if progress['state']=='capturing':
            check_scope()
            for n,leaf in m['current'].items():
                if leaf is None: continue
                if n in family.NAMES: check_scope()
                saved=engine.directory/'custody-originals'/token(n)
                if _leaf(saved) is None:
                    pending('move:'+n)
                    family._move(engine,root/n,saved,leaf)
            check_scope()
            progress['state']='custody_committed';pending(None)
        # Coherent OLD family first; never invoke SQLite between generations.
        order=[n for n in family.NAMES if n in m['old']]+[n for n in m['old'] if n not in family.NAMES]
        check_scope()
        for n in order:
            old=m['old'][n];target=root/n
            if old is not None and n in family.NAMES: check_scope()
            if old is None:
                if os.path.lexists(target): raise cold.RecoveryError('TARGET_CHANGED')
                continue
            if n not in progress['restore']:
                stage=base/'restore-staging'/token(n)
                if n in family.NAMES and j['sqlite']:
                    stage=engine.directory/'sqlite-quarantine'/n
                    expected=old
                else:
                    if not stage.exists():
                        pending('restore-copy:'+n)
                        _copy_blob(engine,base/'old'/token(n),stage,decode=True)
                    expected=_leaf(stage)
                    if expected['sha256']!=old['sha256']: raise cold.RecoveryError('PREIMAGE_CHANGED')
                progress['restore'][n]=expected
                pending('restore:'+n)
            expected=progress['restore'][n]
            if os.path.lexists(target):
                if _leaf(target)!=expected: raise cold.RecoveryError('TARGET_CHANGED')
                continue
            source=(engine.directory/'sqlite-quarantine'/n if n in family.NAMES and j['sqlite'] else base/'restore-staging'/token(n))
            pending('restore:'+n)
            family._move(engine,source,target,expected)
        check_scope()
        progress['state']='restored';pending(None)
        engine._state('rolled_back')
        return engine.status()
