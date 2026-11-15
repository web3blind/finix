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
    raise cold.RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')

def require_stopped_attempt(engine,proof):
    raise cold.RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')

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
    raise cold.RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')
