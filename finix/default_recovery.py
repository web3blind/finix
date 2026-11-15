"""Historical default recovery journal readers. Runtime mutation is retired.

Shared resources are retained, not cloned. Journaled per-file publication is not
an ACID directory transaction. Preimages require separately authorized cleanup.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import re
import uuid
import weakref
from . import core
from .preview_budget import check as _preview_check, PreviewBudgetError
from .recovery_import import _members, RUNTIME_ROOTS, RUNTIME_FILES
from .recovery_import import profile_digest

class RecoveryError(core.FinixError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)

_CAPABILITIES=weakref.WeakKeyDictionary()
class VerifiedImport:
    """Construction alone confers no authority; use verified_import."""
    pass

# Actual SDK writer-fence locks: archive bytes cannot represent live ownership.
CURRENT_COORDINATION_FILES = frozenset({'.backup.lock', '.checkpoints.lock'})
SHARED=RUNTIME_ROOTS|CURRENT_COORDINATION_FILES|frozenset({'plugin-data','shared','checkpoints','desktop',
    'bot-desktop','plugins','desktop-plugins','hooks','agent-hooks','cron','state',
    'shell-hooks-allowlist.json','active_profile','.finix-paused','recovery-custody'})

# Deliberately narrow destructive boundary. Other target members are retained,
# not claimed as a machine clone. Incoming integration code remains parked.
PROFILE_REPLACEMENT_ROOTS = frozenset({'memories', 'skills'})
REPLACEMENT_POLICY = 'profile-context-v1'

def _owned_inventory(root, stack):
    result={}
    for name in sorted(PROFILE_REPLACEMENT_ROOTS):
        path=core.checked_path(root/name)
        if path.exists():
            if not path.is_dir(): raise RecoveryError('TARGET_CONFLICT')
            _retain(stack,path,root)
            # Census locks must be released before a second census in this same
            # exclusive SDK transaction (flock is not reentrant across opens).
            with ExitStack() as census:
                result.update({name+'/'+n:d for n,(_,d) in _inventory(path,census,private_from=root).items()})
    return result

def _kept_inventory(root, entries):
    # Root-member inventory, NOT a recursive census of host installs/cache/keys.
    # Directories are opaque identity-pinned retained subtrees; digest describes
    # this metadata inventory, never their uninspected recursive contents.
    result={}
    for path in sorted(root.iterdir()):
        _preview_check()
        if path.name in PROFILE_REPLACEMENT_ROOTS or path.name in entries: continue
        info=path.lstat()
        if (stat.S_ISREG(info.st_mode) and path.name.casefold() not in RUNTIME_FILES
            and path.name.casefold() not in CURRENT_COORDINATION_FILES
            and path.name not in {'auth.json','finix.key','.backup.lock'}
            and not path.name.startswith('.finix-')):
            result[path.name]={'kind':'file','sha256':core.digest_file(path)}
        else:
            result[path.name]={'kind':'opaque-retained-member','identity':[info.st_dev,info.st_ino]}
    return result

def _workspace_kept(kept):
    """The sole leaf-level CURRENT exception inside replacement roots."""
    result={n:v for n,v in kept.items()
            if type(v) is dict and v.get('kind')=='current-workspace-skill-runtime'}
    if not result:return result
    markers=[n for n in result if n.endswith('/SKILL.md')]
    if len(markers)!=1:raise RecoveryError('INVALID_JOURNAL')
    base=markers[0][:-len('/SKILL.md')]
    if (not base.startswith('skills/') or not _member(base)
        or set(result)!={base+'/SKILL.md',base+'/scripts/google_api.py',base+'/scripts/_hermes_home.py'}):
        raise RecoveryError('INVALID_JOURNAL')
    first=result[markers[0]]
    for name,value in result.items():
        leaf=value.get('leaf')
        if (set(value)!={'kind','leaf','selection','binding'}
            or type(leaf) is not dict or set(leaf)!={'identity','size','sha256'}
            or type(leaf['identity']) is not list or len(leaf['identity'])!=2
            or any(type(i) is not int or i<0 for i in leaf['identity'])
            or type(leaf['size']) is not int or leaf['size']<0 or not _hex(leaf['sha256'])
            or type(value['selection']) is not dict or type(value['binding']) is not list
            or value['selection']!=first['selection'] or value['binding']!=first['binding']):
            raise RecoveryError('INVALID_JOURNAL')
    return result


def _check_current_workspace(p):
    raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')


def _identity(path):
    info=path.stat(follow_symlinks=False)
    return [info.st_dev,info.st_ino]

def _leaf_fd(fd):
    before=os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:
        raise RecoveryError('UNSAFE_LINK')
    digest=hashlib.sha256(); size=0
    os.lseek(fd,0,os.SEEK_SET)
    while chunk:=os.read(fd,1024*1024):
        _preview_check()
        digest.update(chunk);size+=len(chunk)
    after=os.fstat(fd)
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,1)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink) or size!=before.st_size:
        raise RecoveryError('TARGET_CHANGED')
    return dict(identity=[before.st_dev,before.st_ino],size=size,sha256=digest.hexdigest())

def _leaf(path):
    with core.regular_input(path) as fd: return _leaf_fd(fd)

def _sqlite_helper_pin():
    """Whole source bytes at the trusted loaded module origin, not a function subset.

    Relocatable across the existing private detached-package copy; the path and
    native identity are validated locally, never substituted for consent digest.
    """
    from . import sqlite_family
    spec=sqlite_family.__spec__
    expected=Path(__file__).absolute().with_name('sqlite_family.py')
    if spec is None or spec.origin is None or Path(spec.origin).absolute()!=expected:
        raise RecoveryError('PINNED_PACKAGE_CHANGED')
    with ExitStack() as guards:
        _retain(guards,expected.parent,expected.parent)
        with core.regular_input(expected) as fd:
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
            digest=_leaf_fd(fd)['sha256']
    return dict(module='finix.sqlite_family',sha256=digest)


def _check_sqlite_helper(p):
    families=p['sqlite_families']
    if not families:return
    policy=families.get('state.db')
    capture=policy.get('capture_pins') if type(policy) is dict else None
    sealed=capture.get('helper_module') if type(capture) is dict else None
    if (type(sealed) is not dict or set(sealed)!={'module','sha256'}
        or sealed.get('module')!='finix.sqlite_family' or not _hex(sealed.get('sha256'))):
        raise RecoveryError('PINNED_PACKAGE_CHANGED')
    if _sqlite_helper_pin()!=sealed:raise RecoveryError('PINNED_PACKAGE_CHANGED')


def _runtime_executable():
    """Pin the actual process image, not a Windows venv redirector spelling."""
    import sys
    if os.name!='nt':return Path(sys.executable).resolve()
    from . import windows_native as win
    module_name=win.api(win.K,'GetModuleFileNameW',win.W.DWORD,
                        win.W.HMODULE,win.W.LPWSTR,win.W.DWORD)
    buffer=win.C.create_unicode_buffer(32768)
    count=module_name(None,buffer,len(buffer))
    if not 0<count<len(buffer):raise RecoveryError('PINNED_PACKAGE_CHANGED')
    return Path(buffer.value).resolve()


def _sqlite_generation(path, sqlite_temp_dir):
    raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')


def _verify_sqlite_generation(path, sqlite_temp_dir, sealed):
    """Compare without changing the sealed object or granting new consent."""
    from . import sqlite_family
    if type(sealed) is not dict or sealed.get('kind')!=sqlite_family.POLICY:
        raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
    path=core.checked_path(path)
    if _identity(path)!=sealed.get('native_identity'):
        raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
    try:
        current=_sqlite_generation(path,sqlite_temp_dir)
    except OSError:
        raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED') from None
    compared_current=dict(current);compared_sealed=dict(sealed)
    if current.get('protected_content_sha256') is not None and sealed.get('protected_content_sha256') is not None:
        compared_current.pop('snapshot_sha256');compared_sealed.pop('snapshot_sha256')
    if compared_current!=compared_sealed: raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
    return current


def _move_noreplace(a,name,b,destination):
    import ctypes as c
    rename=getattr(c.CDLL(None,use_errno=True),'renameat2',None)
    if rename is None: raise RecoveryError('CONDITIONAL_DELETE_UNSUPPORTED')
    rename.argtypes=[c.c_int,c.c_char_p,c.c_int,c.c_char_p,c.c_uint]
    rename.restype=c.c_int
    if rename(a,os.fsencode(name),b,os.fsencode(destination),1):
        raise RecoveryError('QUARANTINE_CONFLICT')
    os.fsync(a);os.fsync(b)

def _root(root):
    root = core.checked_path(root)
    _private(root)
    return root

def _private(path):
    if os.name=='nt':
        from .windows_native import trusted_directory
        if not trusted_directory(path,private=True): raise RecoveryError('UNSAFE_ACL')
    else:
        info=path.stat(follow_symlinks=False)
        if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise RecoveryError('UNSAFE_ACL')

def _retain(stack,path,private_from):
    """Retain every directory ancestor; never repair somebody else's permissions."""
    path=Path(path)
    if os.name=='nt':
        from . import windows_native as win
        anchor=stack.enter_context(win.directory(path))
        for part,handle in zip([*reversed(path.parents),path],anchor.handles):
            private=part==private_from or private_from in part.parents
            if not win.trusted_handle(handle,private=private,ancestor=not private):
                raise RecoveryError('UNSAFE_ACL')
        return anchor
    from .recovery import _PosixAnchor, _trusted_directory
    try:
        root=Path(private_from)
        # The chain starts at '/'; unsupported lexical anchors must not bypass
        # the owned boundary. Reject, never normalize an alias into authority.
        if root.anchor!='/' or path.anchor!='/':
            raise ValueError('unsupported POSIX anchor')
        if root!=path and root not in path.parents:
            raise ValueError('retained root must contain path')
        # POSIX effective privacy: an exact owner-only root prevents unrelated
        # UIDs traversing SDK-created 0755 descendants. Every component remains
        # no-follow, owned and non-group/other-writable; external checks persist.
        pool=getattr(stack,'_finix_retained_prefixes',None)
        if pool is None:
            pool={}
            stack._finix_retained_prefixes=pool
            # Registered before fd callbacks: clear only after all handles close.
            def clear():
                pool.clear()
                del stack._finix_retained_prefixes
            stack.callback(clear)
        anchor=_PosixAnchor(stack,path,owned_from=root,retained=pool)
        rootfd=anchor.fds[len(root.parts)-1]
        _trusted_directory(rootfd,private=True,owned=True)
        anchor.check()
        _trusted_directory(rootfd,private=True,owned=True)
        return anchor
    except OSError as exc:
        import errno
        if exc.errno in {errno.EMFILE,errno.ENFILE}: raise
        raise RecoveryError('UNSAFE_ANCESTOR') from None
    except Exception: raise RecoveryError('UNSAFE_ANCESTOR') from None

def _sync_directory(path):
    if os.name!='nt':
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)

def _write(path,value):
    if len(json.dumps(value).encode())>MAX_RECORD: raise RecoveryError('JOURNAL_TOO_LARGE')
    core.atomic_json(path,value)
    _sync_directory(path.parent)

def _inventory(root,stack,*,private_from=None):
    if os.name!='nt' and private_from is not None:
        # The existing retained-root guard proves effective POSIX privacy for
        # owned, non-writable SDK descendants beneath the private profile root.
        _retain(stack,root,private_from)
    else:
        _private(root)
    result={}
    for name,path in _members(root,stack):
        _preview_check()
        if not core.safe_name(name) or ':' in name: raise RecoveryError('UNSAFE_PATH')
        with core.regular_input(path) as fd:
            if os.fstat(fd).st_nlink!=1: raise RecoveryError('UNSAFE_LINK')
        result[name]=(path,core.digest_file(path))
    return result

def _copy(source,destination):
    _preview_check()
    core.private_dir(destination.parent)
    with core.regular_input(source) as src,core.regular_output(destination) as dst:
        while chunk:=os.read(src,1024*1024):
            _preview_check()
            view=memoryview(chunk)
            while view:
                written=os.write(dst,view);view=view[written:]
        os.fsync(dst)
    _sync_directory(destination.parent)

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

MAX_RECORD = 16*1024*1024  # byte bound, never a file-count admission ceiling

def _hex(value, length=64):
    return isinstance(value,str) and re.fullmatch('[a-f0-9]{%d}' % length,value) is not None

def _member(name):
    if not isinstance(name,str) or not core.safe_name(name): return False
    parts=name.split('/')
    for part in parts:
        if (not part or part in {'.','..'} or part[-1] in '. '
            or any(c in '<>:"\\\\|?*' or ord(c)<32 for c in part)
            or part.split('.')[0].upper() in {'CON','PRN','AUX','NUL','CONIN$','CONOUT$'}
            or re.fullmatch(r'(COM|LPT)[1-9¹²³]',part.split('.')[0].upper())): return False
    return (parts[0].casefold() not in SHARED and name.casefold() not in RUNTIME_FILES
            and name.casefold() not in {'gateway.sock','auth.json','auth.lock','finix.key'}
            and not parts[0].casefold().startswith('.finix-'))

def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise RecoveryError('INVALID_JOURNAL')
        result[key]=value
    return result

def _load(path):
    try:
        with core.regular_input(path) as fd:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_RECORD:
                raise RecoveryError('INVALID_JOURNAL')
            data=core.read_regular(fd,MAX_RECORD+1)
            if len(data)>MAX_RECORD: raise RecoveryError('INVALID_JOURNAL')
            return json.loads(data,object_pairs_hook=_pairs)
    except RecoveryError: raise
    except Exception: raise RecoveryError('INVALID_JOURNAL') from None

def _validate_plan(p):
    keys={'version','id','root','root_identity','source','source_identity','source_digest',
          'content_digest','epoch','entries','role','retained_source_shared_files','shared_policy',
          'replacement_policy','replacement_roots','target_owned','deletions','kept_target',
          'kept_target_count','kept_target_digest','target_entries','deletion_leaves','sqlite_families'}
    if type(p) is dict and p.get('version')==4: keys.add('runtime_attempt')
    if (type(p) is not dict or set(p)!=keys or type(p['version']) is not int or p['version'] not in (3,4)
        or not _hex(p['id'],32) or not _hex(p['epoch'],32)
        or p['role']!='default' or p['shared_policy']!='preserve-existing-report-source-not-promoted'
        or type(p['retained_source_shared_files']) is not int or p['retained_source_shared_files']<0
        or not _hex(p['content_digest']) or type(p['source_digest']) is not dict
        or not all(isinstance(n,str) and core.safe_name(n) and _hex(d) for n,d in p['source_digest'].items())
        or not all(type(p[k]) is list and len(p[k])==2 and all(type(i) is int and i>=0 for i in p[k])
                   for k in ['root_identity','source_identity'])
        or not all(isinstance(p[k],str) and Path(p[k]).is_absolute() for k in ['root','source'])
        or type(p['entries']) is not dict or 'config.yaml' not in p['entries']):
        raise RecoveryError('INVALID_JOURNAL')
    if (p['replacement_policy']!=REPLACEMENT_POLICY
        or p['replacement_roots']!=sorted(PROFILE_REPLACEMENT_ROOTS)
        or type(p['target_owned']) is not dict or type(p['deletions']) is not dict
        or type(p['kept_target']) is not dict
        or p['kept_target_count']!=len(p['kept_target'])
        or p['kept_target_digest']!=_digest(p['kept_target'])):
        raise RecoveryError('INVALID_JOURNAL')
    for name,digest in p['target_owned'].items():
        if not _member(name) or name.split('/')[0] not in PROFILE_REPLACEMENT_ROOTS or not _hex(digest):
            raise RecoveryError('INVALID_JOURNAL')
    if type(p['deletion_leaves']) is not dict or set(p['deletion_leaves'])!=set(p['deletions']):
        raise RecoveryError('INVALID_JOURNAL')
    for name,leaf in p['deletion_leaves'].items():
        if (type(leaf) is not dict or set(leaf)!={'identity','size','sha256'}
            or type(leaf['identity']) is not list or len(leaf['identity'])!=2
            or any(type(i) is not int or i<0 for i in leaf['identity'])
            or type(leaf['size']) is not int or leaf['size']<0
            or leaf['sha256']!=p['deletions'][name]):
            raise RecoveryError('INVALID_JOURNAL')
    retained=_workspace_kept(p['kept_target'])
    if (any(n in p['entries'] or p['target_owned'].get(n)!=v['leaf']['sha256'] for n,v in retained.items())
        or p['deletions']!={n:d for n,d in p['target_owned'].items() if n not in p['entries'] and n not in retained}):
        raise RecoveryError('INVALID_JOURNAL')
    if (type(p['target_entries']) is not dict or set(p['target_entries'])!=set(p['entries'])
        or any(d is not None and not _hex(d) for d in p['target_entries'].values())):
        raise RecoveryError('INVALID_JOURNAL')
    seen=set()
    for name,digest in p['entries'].items():
        if not _member(name) or not _hex(digest) or name.casefold() in seen:
            raise RecoveryError('INVALID_JOURNAL')
        seen.add(name.casefold())
    for name in seen:
        if any('/'.join(name.split('/')[:i]) in seen for i in range(1,len(name.split('/')))):
            raise RecoveryError('INVALID_JOURNAL')
    source=Path(p['source'])
    if source.parent.parts[-3:]!=('cache','finix','default-copies') or not _hex(source.name,32):
        raise RecoveryError('INVALID_JOURNAL')
    if type(p['sqlite_families']) is not dict:raise RecoveryError('INVALID_JOURNAL')
    _check_sqlite_helper(p)
    from .sqlite_family import validate_policy
    validate_policy(p)
    if p['version']==4:
        from .runtime_attempt import validate_policy
        validate_policy(p)

def verified_import(service,job_id,*,profile_id,activation,name):
    raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')

def prepare(verified_import_capability,canonical_default_root,*,journal_root,intent,runtime_attempt_policy=None):
    raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')

class DefaultRecovery:
    def __init__(self,directory,plan_digest):
        self.directory=core.checked_path(directory)
        self.plan_digest=plan_digest
        try:
            plan=self._plan()
            root=_root(plan['root'])
            if self.directory!=root/'cache/finix/default-recovery'/plan['id']:
                raise RecoveryError('UNSAFE_JOURNAL')
            if len(plan['id'])!=32 or any(c not in '0123456789abcdef' for c in plan['id']):
                raise RecoveryError('UNSAFE_JOURNAL')
            _private(self.directory)
            with ExitStack() as guards:
                _retain(guards,self.directory,root)
            if _identity(root)!=plan['root_identity']: raise RecoveryError('ROOT_CHANGED')
            for name in plan['entries']:
                if not core.safe_name(name) or ':' in name or name.split('/')[0].casefold() in SHARED:
                    raise RecoveryError('UNSAFE_JOURNAL')
        except RecoveryError: raise
        except Exception: raise RecoveryError('INVALID_JOURNAL') from None
    def status(self):
        value=_load(self.directory/'status.json')
        if (type(value) is not dict or set(value)!={'state','error'}
            or value['state'] not in {'prepared','publishing','published_offline','rolling_back','rolled_back','rollback_failed'}
            or value['error'] not in {None,'ROLLBACK_FAILED'}):
            raise RecoveryError('INVALID_JOURNAL')
        return value
    def public_plan(self):
        p=self._plan()
        result={k:p[k] for k in ['id','role','content_digest','retained_source_shared_files','shared_policy',
            'replacement_policy','replacement_roots','target_owned','deletions',
            'kept_target','kept_target_count','kept_target_digest']}
        if p['version']==4: result['runtime_attempt']=p['runtime_attempt']
        return result
    def consent(self):
        p=self._plan()
        return dict(plan_digest=self.plan_digest,root_identity=p['root_identity'],
                    content_digest=p['content_digest'],epoch=p['epoch'],intent='replace-real-default')
    def _state(self,state,error=None):
        if state=='published_offline':
            p=self._plan()
            if p['sqlite_families']:
                from .sqlite_family import admit_published
                admit_published(self,p,self._journal(p)['sqlite'])
        _write(self.directory/'status.json',dict(state=state,error=error))
    def _fence(self,p,stack):
        root=Path(p['root'])
        _retain(stack,self.directory,root)
        if _identity(root)!=p['root_identity']: raise RecoveryError('ROOT_CHANGED')
        parents={root, self.directory/'payload'}
        for name in dict(p['entries'],**p['deletions']):
            parents.add((root/name).parent)
            if name in p['entries']: parents.add((self.directory/'payload'/name).parent)
            if (self.directory/'preimages').exists():
                parents.add((self.directory/'preimages'/name).parent)
        for parent in sorted(parents,key=lambda x:len(x.parts)):
            existing=parent
            while not existing.exists(): existing=existing.parent
            _retain(stack,existing,root)
        self._parent_guards={}
        for parent in sorted(parents,key=lambda x:len(x.parts)):
            core.private_dir(parent)
            self._parent_guards[parent]=_retain(stack,parent,root)
        stack.callback(lambda: setattr(self,'_parent_guards',{}))
    def _publish(self,source,target):
        core.private_dir(target.parent)
        temporary=target.parent/('.finix-promote-'+uuid.uuid4().hex)
        _copy(source,temporary)
        if getattr(self,'_restoring',False):
            record=self.directory/'restored-leaves.json'
            leaves=_load(record) if record.exists() else {}
            leaves[str(target)]=_leaf(temporary)
            _write(record,leaves)
        if os.name=='nt':
            from .windows_native import publish_directory
            no_clobber=getattr(self,'_restoring',False) and target.relative_to(Path(self._plan()['root'])).as_posix() in self._plan()['deletions']
            publish_directory(temporary,target,replace=not no_clobber)
        else:
            guard=getattr(self,'_parent_guards',{}).get(target.parent)
            if guard is None: raise RecoveryError('ANCESTOR_NOT_RETAINED')
            guard.check()
            no_clobber=getattr(self,'_restoring',False) and target.relative_to(Path(self._plan()['root'])).as_posix() in self._plan()['deletions']
            if no_clobber:
                os.link(temporary.name,target.name,src_dir_fd=guard.fd,dst_dir_fd=guard.fd,follow_symlinks=False)
                os.unlink(temporary.name,dir_fd=guard.fd)
            else:
                os.replace(temporary.name,target.name,src_dir_fd=guard.fd,dst_dir_fd=guard.fd)
            os.fsync(guard.fd)
            guard.check()
    def _delete(self,target):
        p=self._plan()
        name=target.relative_to(Path(p['root'])).as_posix()
        expected=p['deletion_leaves'].get(name)
        if expected is None:
            expected=getattr(self,'_rollback_delete',{}).get(name)
        if expected is None: raise RecoveryError('UNAUTHORIZED_DELETE')
        guard=getattr(self,'_parent_guards',{}).get(target.parent)
        if guard is None: raise RecoveryError('ANCESTOR_NOT_RETAINED')
        if os.name=='nt':
            from . import windows_native as win
            handle=win.relative_open(guard.handle,target.name,delete=True,share_write=False)
            import msvcrt
            fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
            try:
                if _leaf_fd(fd)!=expected: raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
                flag=win.W.BOOL(True)
                dispose=win.api(win.K,'SetFileInformationByHandle',win.W.BOOL,
                    win.W.HANDLE,win.C.c_int,win.C.c_void_p,win.W.DWORD)
                if win.information(handle).links!=1:
                    raise RecoveryError('UNSAFE_LINK')
                if not dispose(handle,4,win.C.byref(flag),win.C.sizeof(flag)):
                    raise RecoveryError('DELETE_FAILED')
            finally: os.close(fd)
        else:
            # Atomic capture, not a last-stat/pathname-unlink approximation.
            quarantine=core.private_dir(self.directory/'quarantine')
            with ExitStack() as stack:
                q=_retain(stack,quarantine,Path(p['root']))
                captured=hashlib.sha256(name.encode()).hexdigest()
                guard.check();q.check()
                _move_noreplace(guard.fd,target.name,q.fd,captured)
                try:
                    fd=os.open(captured,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=q.fd)
                    try:
                        if _leaf_fd(fd)!=expected: raise RecoveryError('TARGET_CHANGED_REPREPARE_REQUIRED')
                    finally: os.close(fd)
                except BaseException:
                    # Never clobber a newly created basename during restoration.
                    _move_noreplace(q.fd,captured,guard.fd,target.name)
                    raise
                os.unlink(captured,dir_fd=q.fd);os.fsync(q.fd)
                guard.check();q.check()
    def apply(self,consent):
        raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')
    def _journal(self,p):
        j=_load(self.directory/'journal.json')
        from . import sqlite_family
        sqlite_family.validate_journal(self,p,j)
        names=[n for n in dict(p['entries'],**p['deletions']) if n not in p['sqlite_families']]
        expected=dict(p['target_entries'],**p['deletions'])
        if j.get('sqlite'):
            leaf=j['sqlite']['manifest']['members']['state.db']
            expected['state.db']=leaf['sha256'] if leaf else None
        if (type(j) is not dict or set(j)!={'version','plan_digest','old','pending','published','sqlite'}
            or type(j['version']) is not int or j['version']!=(3 if p['version']==4 else 2) or j['plan_digest']!=self.plan_digest
            or type(j['old']) is not dict or j['old']!=expected
            or any(d is not None and not _hex(d) for d in j['old'].values())
            or type(j['pending']) is not list or type(j['published']) is not list
            or len(j['pending'])>1 or j['published']!=names[:len(j['published'])]
            or len(j['published'])>len(names)
            or (j['pending'] and j['pending']!=names[len(j['published']):len(j['published'])+1])):
            raise RecoveryError('INVALID_JOURNAL')
        for name in names:
            target=core.checked_path(Path(p['root'])/name)
            if target.exists():
                if target.lstat().st_nlink!=1: raise RecoveryError('UNSAFE_LINK')
                with core.regular_input(target) as fd:
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                        raise RecoveryError('UNSAFE_LINK')
        # Verify every retained preimage before restoring even the first member.
        for name,digest in j['old'].items():
            if digest is not None:
                image=self.directory/'preimages'/name
                with core.regular_input(image) as fd:
                    if os.fstat(fd).st_nlink!=1: raise RecoveryError('UNSAFE_LINK')
                if core.digest_file(image)!=digest: raise RecoveryError('PREIMAGE_CHANGED')
        return j
    def _restore(self,root,journal,root_anchor=None):
        self._state('rolling_back')
        p=self._plan()
        if journal.get('sqlite'):
            from .sqlite_family import restore
            restore(self,p,journal)
        self._restoring=True
        names=list(dict.fromkeys(journal['published']+journal['pending']))
        for name in reversed(names):
            target=core.checked_path(root/name)
            old=journal['old'][name]
            if name in p['deletions']:
                captured=self.directory/'quarantine'/hashlib.sha256(name.encode()).hexdigest()
                if os.path.lexists(captured):
                    # Reattach a capture after death using NOREPLACE, including
                    # unexpected objects: they are preserved, never overwritten.
                    if os.name=='nt': raise RecoveryError('UNSAFE_JOURNAL')
                    with ExitStack() as stack:
                        q=_retain(stack,captured.parent,root)
                        guard=self._parent_guards[target.parent]
                        _move_noreplace(q.fd,captured.name,guard.fd,target.name)
                if os.path.lexists(target):
                    record=self.directory/'restored-leaves.json'
                    permitted=[p['deletion_leaves'][name]]
                    if record.exists(): permitted.append(_load(record).get(str(target)))
                    if _leaf(target) not in permitted: raise RecoveryError('TARGET_CHANGED')
                    continue
            if old is None:
                if target.exists():
                    if core.digest_file(target)!=p['entries'][name]: raise RecoveryError('TARGET_CHANGED')
                    self._rollback_delete={name:_leaf(target)}
                    self._delete(target)
            else:
                image=self.directory/'preimages'/name
                if core.digest_file(image)!=old: raise RecoveryError('PREIMAGE_CHANGED')
                # Bypass a failed publication callback only at the private file
                # primitive, not an SDK ownership/readiness or security boundary.
                DefaultRecovery._publish(self,image,target)
                if core.digest_file(target)!=old: raise RecoveryError('ROLLBACK_VERIFY_FAILED')
        self._restoring=False
    def rollback(self,consent):
        raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')
    def rollback_attempt(self,consent,attempt):
        raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')
    def _plan(self):
        p=_load(self.directory/'plan.json')
        if not _hex(self.plan_digest) or _digest(p)!=self.plan_digest: raise RecoveryError('PLAN_CHANGED')
        _validate_plan(p)
        return p
