"""Versioned SQLite family primitives, never a lifecycle/writer reservation.

Call only under the caller's retained SDK exclusive maintenance reservation.
SHM is retained for physical rollback, not interpreted as committed-data authority.
"""
import os
import stat
import struct
from pathlib import Path
from . import core

NAMES = ('state.db','state.db-wal','state.db-shm','state.db-journal')
POLICY = 'sqlite-consistent-v2-aux-lifecycle'


def _admitted_leaf(path):
    from .default_recovery import RecoveryError, _leaf_fd
    with core.regular_input(path) as fd:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise RecoveryError('UNSAFE_LINK')
        if os.name=='nt':
            import msvcrt
            from .windows_native import trusted_handle
            if not trusted_handle(msvcrt.get_osfhandle(fd),private=True):
                raise RecoveryError('UNSAFE_ACL')
        elif info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o022:
            raise RecoveryError('UNSAFE_ACL')
        return _leaf_fd(fd)


def _wal_metadata(path, members):
    """Bounded streaming WAL header/frame checks; SQLite may ignore junk WAL."""
    from .default_recovery import RecoveryError
    def bad(): raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
    main=members['state.db'];wal=members['state.db-wal'];shm=members['state.db-shm']
    if main is None:
        if wal is not None or shm is not None: bad()
        return
    with core.regular_input(path) as fd: header=os.read(fd,100)
    if len(header)!=100 or header[:16]!=b'SQLite format 3\0': bad()
    pagesize=int.from_bytes(header[16:18],'big'); pagesize=65536 if pagesize==1 else pagesize
    if pagesize<512 or pagesize>65536 or pagesize&(pagesize-1): bad()
    if shm is not None and (wal is None or shm['size']<32768 or shm['size']%32768): bad()
    if wal is None or wal['size']==0: return
    with core.regular_input(Path(str(path)+'-wal')) as fd:
        h=os.read(fd,32)
        if len(h)!=32: bad()
        magic,version,ps=struct.unpack('>III',h[:12])
        if magic not in (0x377f0682,0x377f0683) or version!=3007000 or ps!=pagesize: bad()
        endian='>' if magic&1 else '<'
        def checksum(data,s=(0,0)):
            a,b=s
            words=struct.unpack(endian+str(len(data)//4)+'I',data)
            for i in range(0,len(words),2):
                a=(a+words[i]+b)&0xffffffff
                b=(b+words[i+1]+a)&0xffffffff
            return a,b
        state=checksum(h[:24])
        if state!=struct.unpack('>II',h[24:32]): bad()
        if (wal['size']-32)%(24+pagesize): bad()
        while frame:=os.read(fd,24):
            if len(frame)!=24: bad()
            data=os.read(fd,pagesize)
            if len(data)!=pagesize: bad()
            if frame[8:16]!=h[16:24]:
                # WAL reset can retain old, salt-separated physical tail frames.
                break
            if int.from_bytes(frame[:4],'big')==0: bad()
            state=checksum(data,checksum(frame[:8],state))
            if state!=struct.unpack('>II',frame[16:24]): bad()


def physical(path):
    from .default_recovery import RecoveryError, _leaf
    path=Path(path)
    allowed=set(NAMES)
    for child in path.parent.iterdir():
        if (child.name.casefold()=='state.db' or child.name.casefold().startswith('state.db-')) and child.name not in allowed:
            raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
    result={}
    for name in NAMES:
        member=path.parent/name
        if not os.path.lexists(member):
            result[name]=None
            continue
        if name.endswith('-journal'):
            raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
        result[name]=_admitted_leaf(member)
    _wal_metadata(path,result)
    return result


def prepare_policy(root, entries, directory):
    from .default_recovery import RecoveryError, _sqlite_generation
    if any(n in entries for n in NAMES[1:]):
        raise RecoveryError('UNSUPPORTED_SQLITE_TARGET')
    if 'state.db' not in entries: return {}
    members=physical(root/'state.db')
    if members['state.db'] is None:
        if any(members.values()): raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
        descriptor=None
    else:
        descriptor=_sqlite_generation(root/'state.db',core.private_dir(directory/'sqlite-temp'))
    pins=(descriptor if descriptor is not None else
          _sqlite_generation(directory/'payload/state.db',core.private_dir(directory/'sqlite-temp')))['producer_pins']
    return {'state.db':dict(policy=POLICY,names=list(NAMES),generation=descriptor,
                           capture_pins=pins,shm_authority='non-data-runtime-derived')}


def validate_policy(p):
    from .default_recovery import RecoveryError, _hex
    families=p['sqlite_families']
    if type(families) is not dict or set(families)!=({'state.db'} if 'state.db' in p['entries'] else set()):
        raise RecoveryError('INVALID_JOURNAL')
    if any(n in p['entries'] for n in NAMES[1:]): raise RecoveryError('INVALID_JOURNAL')
    if not families: return
    policy=families['state.db']
    if (type(policy) is not dict or set(policy)!={'policy','names','generation','capture_pins','shm_authority'}
        or policy['policy']!=POLICY or policy['names']!=list(NAMES)
        or type(policy['capture_pins']) is not dict
        or policy['shm_authority']!='non-data-runtime-derived'):
        raise RecoveryError('INVALID_JOURNAL')
    g=policy['generation']
    if g is None:
        if p['target_entries']['state.db'] is not None: raise RecoveryError('INVALID_JOURNAL')
        return
    if type(g) is not dict or g.get('producer_pins')!=policy['capture_pins']: raise RecoveryError('INVALID_JOURNAL')
    if (type(g) is not dict or set(g)!={'kind','native_identity','snapshot_sha256',
         'capture_algorithm_version','producer_pins','family_names','shm_authority'}
        or g['kind']!=POLICY or g['family_names']!=list(NAMES)
        or g['shm_authority']!='non-data-runtime-derived'
        or g['capture_algorithm_version']!='guarded-disk-online-backup-v1'
        or not _hex(g['snapshot_sha256']) or type(g['native_identity']) is not list
        or len(g['native_identity'])!=2 or any(type(i) is not int or i<0 for i in g['native_identity'])
        or type(g['producer_pins']) is not dict):
        raise RecoveryError('INVALID_JOURNAL')


def bind(engine,p):
    from .default_recovery import _verify_sqlite_generation, _sqlite_generation, _copy, _leaf, _digest, RecoveryError, _check_sqlite_helper
    _check_sqlite_helper(p)
    root=Path(p['root'])
    sealed=p['sqlite_families']['state.db']['generation']
    if sealed is not None:
        _verify_sqlite_generation(root/'state.db',core.private_dir(engine.directory/'sqlite-temp'),sealed)
    else:
        current=_sqlite_generation(engine.directory/'payload/state.db',core.private_dir(engine.directory/'sqlite-temp'))
        if current['producer_pins']!=p['sqlite_families']['state.db']['capture_pins']:
            raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
    members=physical(root/'state.db')  # reader CLOSED before physical authority is bound
    if sealed is None and any(members.values()): raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
    preimages=core.private_dir(engine.directory/'preimages')
    for name,leaf in members.items():
        if leaf is not None:
            image=preimages/name
            if image.exists():
                if core.digest_file(image)!=leaf['sha256']: raise RecoveryError('PREIMAGE_CHANGED')
            else: _capture_copy(engine,root/name,image)
            if core.digest_file(image)!=leaf['sha256']: raise RecoveryError('TARGET_CHANGED')
    staging=core.private_dir(engine.directory/'sqlite-staging')
    incoming=staging/'state.db'
    if not incoming.exists(): _capture_copy(engine,engine.directory/'payload/state.db',incoming)
    staged=_leaf(incoming)
    if staged['sha256']!=p['entries']['state.db']: raise RecoveryError('PAYLOAD_CHANGED')
    # Tie the raw copies to the SAME sealed committed view, not merely a later
    # stable physical generation. The second reader is closed before equality.
    if sealed is not None:
        _verify_sqlite_generation(root/'state.db',core.private_dir(engine.directory/'sqlite-temp'),sealed)
    if physical(root/'state.db')!=members: raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
    manifest=dict(version=1,plan_digest=engine.plan_digest,policy=POLICY,
                  members=members,incoming=staged,shm_authority='non-data-runtime-derived')
    return dict(manifest=manifest,manifest_digest=_digest(manifest),pending=None,
                captured=[],published=False,restored=[])


def _capture_copy(engine,source,target):
    """Unbound partial private copies never occupy an authoritative basename.

    Child death may retain an opaque private scratch leaf, not a corrupt final
    preimage. Retrying creates another exclusive leaf and never overwrites one.
    """
    import uuid
    from contextlib import ExitStack
    from .default_recovery import _copy, _retain, _move_noreplace
    temporary=target.parent/('.sqlite-copy-'+uuid.uuid4().hex)
    _copy(source,temporary)
    with ExitStack() as stack:
        parent=_retain(stack,target.parent,Path(engine._plan()['root']))
        if os.name=='nt':
            from .windows_native import publish_directory
            publish_directory(temporary,target,replace=False)
        else:
            parent.check()
            _move_noreplace(parent.fd,temporary.name,parent.fd,target.name)
            parent.check()


def validate_journal(engine,p,j):
    from .default_recovery import RecoveryError, _digest, _hex
    def bad(): raise RecoveryError('INVALID_JOURNAL')
    if type(j) is not dict or 'sqlite' not in j: bad()
    f=j['sqlite']
    if not p['sqlite_families']:
        if f is not None: bad()
        return
    if (type(f) is not dict or set(f)!={'manifest','manifest_digest','pending','captured','published','restored'}
        or type(f['manifest']) is not dict or f['manifest_digest']!=_digest(f['manifest'])): bad()
    m=f['manifest']
    if (set(m)!={'version','plan_digest','policy','members','incoming','shm_authority'}
        or m['version']!=1 or m['plan_digest']!=engine.plan_digest or m['policy']!=POLICY
        or m['shm_authority']!='non-data-runtime-derived'
        or type(m['members']) is not dict or set(m['members'])!=set(NAMES)
        or type(f['published']) is not bool): bad()
    for leaf in [*m['members'].values(),m['incoming']]:
        if leaf is None: continue
        if (type(leaf) is not dict or set(leaf)!={'identity','size','sha256'}
            or type(leaf['identity']) is not list or len(leaf['identity'])!=2
            or any(type(i) is not int or i<0 for i in leaf['identity'])
            or type(leaf['size']) is not int or leaf['size']<0 or not _hex(leaf['sha256'])): bad()
    if m['incoming'] is None or m['incoming']['sha256']!=p['entries']['state.db']: bad()
    sealed=p['sqlite_families']['state.db']['generation']
    if sealed is None:
        if any(m['members'].values()): bad()
    elif m['members']['state.db'] is None or m['members']['state.db']['identity']!=sealed['native_identity']: bad()
    if m['members']['state.db-journal'] is not None: bad()
    for key in ('captured','restored'):
        if (type(f[key]) is not list or len(set(f[key]))!=len(f[key])
            or any(n not in NAMES for n in f[key])): bad()
    if f['pending'] is not None and f['pending'] not in ['publish','remove-incoming']+['capture:'+n for n in NAMES]+['restore:'+n for n in NAMES]: bad()
    for name,leaf in m['members'].items():
        if leaf is not None and core.digest_file(engine.directory/'preimages'/name)!=leaf['sha256']:
            raise RecoveryError('PREIMAGE_CHANGED')


def _move(engine,source,target,expected):
    """Capture first with NOREPLACE; verify exact leaf, never overwrite conflicts."""
    from contextlib import ExitStack
    from .default_recovery import _retain, _move_noreplace, _leaf, RecoveryError
    with ExitStack() as stack:
        a=_retain(stack,source.parent,Path(engine._plan()['root']))
        b=_retain(stack,target.parent,Path(engine._plan()['root']))
        if _admitted_leaf(source)!=expected: raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
        incoming=source==engine.directory/'sqlite-staging/state.db'
        if incoming:
            p=engine._plan();f=engine._journal(p)['sqlite']
            actual=_placement(engine,p,f)
            if (expected!=f['manifest']['incoming']
                or any(leaf is not None or saved!=f['manifest']['members'][name]
                       for name,(leaf,saved) in actual.items())):
                raise RecoveryError('TARGET_CHANGED')
        if os.name=='nt':
            from .windows_native import publish_directory
            publish_directory(source,target,replace=False)
        else:
            a.check();b.check()
            _move_noreplace(a.fd,source.name,b.fd,target.name)
        if _admitted_leaf(target)!=expected: raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')


def _placement(engine,p,f):
    """Validate ALL physical names before doing even one rollback mutation."""
    from .default_recovery import _leaf, RecoveryError
    root=Path(p['root']); q=engine.directory/'sqlite-quarantine'
    m=f['manifest']; actual={}
    for child in root.iterdir():
        if child.name.casefold().startswith('state.db-') and child.name not in NAMES:
            raise RecoveryError('TARGET_CHANGED')
    for name,old in m['members'].items():
        target=root/name; captured=q/name
        leaf=_admitted_leaf(target) if os.path.lexists(target) else None
        saved=_admitted_leaf(captured) if os.path.lexists(captured) else None
        if saved is not None and saved!=old: raise RecoveryError('TARGET_CHANGED')
        allowed=[None,old]
        if name=='state.db': allowed.append(m['incoming'])
        if leaf not in allowed: raise RecoveryError('TARGET_CHANGED')
        if saved is not None and leaf==old: raise RecoveryError('TARGET_CHANGED')
        if old is not None and saved is None and leaf!=old:
            raise RecoveryError('TARGET_CHANGED')
        actual[name]=(leaf,saved)
    return actual


def admit_published(engine,p,f):
    """Checked publication admission, NOT exclusion of arbitrary same-UID writers.

    No SQLite connection is opened over an unadmitted family. The caller retains
    the real SDK exclusive-writers reservation through terminal publication.
    """
    from .default_recovery import RecoveryError
    actual=_placement(engine,p,f)
    if (actual['state.db'][0]!=f['manifest']['incoming']
        or any(actual[n][0] is not None for n in NAMES[1:])
        or any(actual[n][1]!=old for n,old in f['manifest']['members'].items())
        or os.path.lexists(engine.directory/'sqlite-staging/state.db')):
        raise RecoveryError('TARGET_CHANGED')


def _save(engine,j,pending=None):
    from .default_recovery import _write
    j['sqlite']['pending']=pending
    _write(engine.directory/'journal.json',j)


def publish(engine,p,j):
    from .default_recovery import RecoveryError, _check_sqlite_helper
    _check_sqlite_helper(p)
    f=j['sqlite'];m=f['manifest'];root=Path(p['root'])
    q=core.private_dir(engine.directory/'sqlite-quarantine')
    # All auxiliaries leave BEFORE incoming main can enter. Never open SQLite here.
    for name in (*NAMES[1:],NAMES[0]):
        actual=_placement(engine,p,f)
        old=m['members'][name]
        if old is not None:
            _save(engine,j,'capture:'+name)
            _move(engine,root/name,q/name,old)
            f['captured'].append(name)
            _save(engine,j)
    _placement(engine,p,f)
    _save(engine,j,'publish')
    _move(engine,engine.directory/'sqlite-staging/state.db',root/'state.db',m['incoming'])
    admit_published(engine,p,f)
    f['published']=True
    _save(engine,j)


def restore(engine,p,j):
    from .default_recovery import _check_sqlite_helper
    _check_sqlite_helper(p)
    f=j['sqlite'];m=f['manifest'];root=Path(p['root']);q=engine.directory/'sqlite-quarantine'
    actual=_placement(engine,p,f)
    if actual['state.db'][0]==m['incoming']:
        _save(engine,j,'remove-incoming')
        engine._rollback_delete={'state.db':m['incoming']}
        engine._delete(root/'state.db')
        _save(engine,j)
    # Old main first, then OLD companions, under the still-held SDK reservation.
    for name in NAMES:
        actual=_placement(engine,p,f)
        old=m['members'][name]
        if old is not None and actual[name][1] is not None:
            _save(engine,j,'restore:'+name)
            _move(engine,q/name,root/name,old)
        if name not in f['restored']: f['restored'].append(name)
        _save(engine,j)
