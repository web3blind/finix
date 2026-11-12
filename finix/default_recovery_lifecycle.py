"""Narrow true-default maintenance controller. Shared autoruns are unsupported.

Only server-created job IDs cross the worker boundary. The cold engine retains
its own exclusive SDK maintenance reservation; this wrapper never nests it.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from . import core, default_recovery as cold
from .recovery_import import _yaml_mapping, _read, MAX_CONFIG

RecoveryError = cold.RecoveryError


def _sdk_pins():
    from .recovery_activation import LIFECYCLE_PINS
    pins={k:v for k,v in LIFECYCLE_PINS.items() if k in ('hermes_cli.gateway_runtime_start','hermes_cli.gateway_runtime_discovery','gateway.control_socket')}
    pins.update({'hermes_cli.data_cleanup_holders':'a52d5149ff68ee2189a0d792e1d606d58dc26d8d1febc09ecb55400ec804c9c8',
                 'gateway.runtime_ownership':'5c563b48fd9897bcf348fab1bcf74a7a094d88118ca8071b07189ffeeb5fbf51'})
    if os.name=='nt': pins['gateway.runtime_bootstrap_windows']='7f39768e31e6082714304b32d75c175e3534d23d07aa1b1b31ac9e645668e32f'
    for name,digest in pins.items():
        spec=importlib.util.find_spec(name)
        if spec is None or hashlib.sha256(Path(spec.origin).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=digest:
            raise RecoveryError('SDK_VERSION_NOT_SUPPORTED')


def _config(home):
    path=home/'config.yaml'
    return _yaml_mapping(_read(path,MAX_CONFIG)) if path.exists() else {}


def _inert(config, trusted_finix=False):
    # Intentionally a narrow, explicit configuration domain, not a universal
    # blank-role assertion. Unknown configurations require separate admission.
    known={'gateway','platforms','profile_routes','plugins','hooks','skills',
           'mcp_servers','secrets','cron','model','display','telemetry','auth','security'}
    if set(config)-known: return False
    # Stock credential_sources / pm.install opt-outs only: these disable
    # borrowing external logins and acquiring executable dependencies.
    # Do not admit defaults, truthy/coercible values or unrelated policy fields.
    for block,field in (('auth','adopt_external_logins'),('security','allow_lazy_installs')):
        if block in config:
            value=config[block]
            if type(value) is not dict or set(value)!={field} or value[field] is not False: return False
    # Pinned SDK config_defaults.display: language/compact are presentation only.
    display=config.get('display',{})
    if type(display) is not dict or set(display)-{'language','compact'}: return False
    if 'language' in display and type(display['language']) is not str: return False
    if 'compact' in display and type(display['compact']) is not bool: return False
    # shared_metrics_consent.set_answer(False, False), OFFER_VERSION=2 only.
    if 'telemetry' in config:
        telemetry=config['telemetry']
        if type(telemetry) is not dict or set(telemetry)!={'shared_metrics'}: return False
        metrics=telemetry['shared_metrics']
        if type(metrics) is not dict or set(metrics)!={'enabled','send','offer_version'}: return False
        if metrics['enabled'] is not False or metrics['send'] is not False: return False
        if type(metrics['offer_version']) is not int or metrics['offer_version']!=2: return False
    model=config.get('model')
    if model is not None and not isinstance(model,(str,dict)): return False
    if isinstance(model,dict) and (set(model)-{'default'} or (model.get('default') is not None and not isinstance(model['default'],str))): return False
    for key in ('hooks','mcp_servers','secrets'):
        value=config.get(key,{})
        if type(value) is not dict or value: return False
    skills=config.get('skills',{})
    if type(skills) is not dict or set(skills)-{'auto_load'} or skills.get('auto_load',[])!=[]: return False
    plugins=config.get('plugins',{})
    if type(plugins) is not dict or set(plugins)-{'enabled'}: return False
    if plugins.get('enabled',[]) not in ([],['finix'] if trusted_finix else []): return False
    cron=config.get('cron',{})
    if type(cron) is not dict or set(cron)-{'provider'} or cron.get('provider','') not in ('','builtin','in-process','inprocess'): return False
    gateway=config.get('gateway',{})
    if type(gateway) is not dict or set(gateway)-{'standalone','multiplex_profiles','platforms','profile_routes'}: return False
    for key in ('standalone','multiplex_profiles'):
        if key in gateway and type(gateway[key]) is not bool: return False
    for parent in (config,gateway):
        if parent.get('profile_routes',[])!=[]: return False
        platforms=parent.get('platforms',{})
        if type(platforms) is not dict: return False
        for name,block in platforms.items():
            if type(block) is not dict or set(block)-{'enabled'}: return False
            if name=='local':
                if type(block.get('enabled',True)) is not bool: return False
            elif block.get('enabled') is not False: return False
    return True


def _empty_kanban(path):
    """Admit only the reviewed SDK's pristine store, without initializing it.

    A read transaction includes committed WAL data; immutable=1 would not.
    Unknown schema (including triggers/views) and any stored row fail closed.
    Native no-follow single-link handles remain held through SQLite readback.
    """
    import sqlite3
    from contextlib import closing
    pins={
        'hermes_cli.kanban_db':'fee49c5e11e86154d4946713b6c9e31b95b507d5394e8197784367d42739ca4e',
        'hermes_cli.kanban_db_connect':'4b6e14bd4796db77c255d8267ff2ae24cc463db25a4ee80abe3d80d83d29ccbc',
    }
    for name,digest in pins.items():
        spec=importlib.util.find_spec(name)
        if spec is None or hashlib.sha256(Path(spec.origin).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=digest:
            raise ValueError('unknown producer')
    with ExitStack() as stack:
        handles=[]
        for member in [path,Path(str(path)+'-wal'),Path(str(path)+'-shm'),Path(str(path)+'-journal')]:
            if member==path or member.exists():
                fd=stack.enter_context(core.regular_input(member))
                if os.fstat(fd).st_nlink!=1: raise ValueError('linked store')
                handles.append((member,fd,os.fstat(fd)))
        def check():
            for member,fd,before in handles:
                now=member.stat(follow_symlinks=False)
                held=os.fstat(fd)
                if held.st_nlink!=1 or (now.st_dev,now.st_ino)!=(before.st_dev,before.st_ino):
                    raise ValueError('changed store')
        check()
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            db.execute('pragma query_only=on')
            db.execute('pragma trusted_schema=off')
            db.execute('begin')
            if db.execute('pragma application_id').fetchone()!=(0,) or db.execute('pragma user_version').fetchone()!=(0,):
                raise ValueError('unknown version')
            rows=db.execute('select type,name,tbl_name,sql from sqlite_master order by type,name').fetchall()
            # Derived from stock connect's SCHEMA_SQL plus additive migrations,
            # and checked against a newly initialized real SDK store in tests.
            if hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()!='7b2348c7cddd2ea0ac2769783bae9450338aa69c09a7daa040629ce1b5b4f9d8':
                raise ValueError('unknown schema')
            if db.execute('pragma integrity_check').fetchall()!=[('ok',)]: raise ValueError('corrupt store')
            for kind,name,_,_ in rows:
                if kind=='table' and db.execute('select 1 from "'+name.replace('"','""')+'" limit 1').fetchone() is not None:
                    raise ValueError('nonempty store')
            check()


def preflight(root, *, trusted_finix=False):
    """Read-only admission of ALL installed siblings, a superset of tick scope.

    No scheduler provider is invented or patched; stock cron may tick empty
    sibling stores. Nonempty hook/cron/autorun stores fail before any stop.
    """
    root=cold._root(root)
    _sdk_pins()
    from hermes_cli.profiles import profiles_to_serve
    homes={Path(p) for _,p in profiles_to_serve(True,include_standalone=True,include_parked=True)}
    homes.add(root)
    result={}
    for home in sorted(homes):
        error='DEFAULT_AUTORUN_NOT_SUPPORTED' if os.path.samefile(home,root) else 'SIBLING_AUTORUN_NOT_SUPPORTED'
        try:
            with ExitStack() as anchors:
                cold._retain(anchors,home,root)
                config=_config(home)
                if not _inert(config,trusted_finix and home==root): raise ValueError()
                for name in ('hooks','agent-hooks','autorun'):
                    path=home/name
                    if path.exists() and (not path.is_dir() or any(path.iterdir())): raise ValueError()
                if (home/'kanban.db').exists(): _empty_kanban(home/'kanban.db')
                board=home/'kanban'
                if board.exists():
                    if not board.is_dir(): raise ValueError()
                    for child in board.iterdir():
                        if child.name!='.dispatcher.lock': raise ValueError()
                        with core.regular_input(child) as fd:
                            if os.fstat(fd).st_nlink!=1: raise ValueError()
                for name in ('.env','mcp.json'):
                    path=home/name
                    if path.exists() and _read(path,MAX_CONFIG).strip(): raise ValueError()
                jobs=home/'cron/jobs.json'
                value=cold._load(jobs) if jobs.exists() else {'jobs':[]}
                if type(value) is not dict or set(value)-{'jobs','version'} or 'jobs' not in value or value['jobs']!=[]: raise ValueError()
                if 'version' in value and type(value['version']) is not int: raise ValueError()
                result[str(home)]=cold._digest(config)
        except Exception:
            raise RecoveryError(error) from None
    return result


@contextmanager
def writer_fence(root):
    """Kernel writer locks excluding gateway.lock, owned by the inner engine."""
    from hermes_cli.data_cleanup_holders import _refuse_backend_writers, _refuse_cron_writers
    from hermes_cli.active_sessions import _FileLock, _prune_dead, _read_entries
    from hermes_cli.runtime_state import _lock
    from tools.checkpoint_pruning import store_lock
    _refuse_backend_writers(root)
    with ExitStack() as stack:
        stack.enter_context(store_lock(root/'checkpoints'))
        core.private_dir(root/'runtime')
        stack.enter_context(_FileLock(root/'runtime/active_sessions.lock'))
        if _prune_dead(_read_entries(root/'runtime/active_sessions.json',strict=True),strict=True):
            raise RecoveryError('WRITERS_BUSY')
        for path in (root/'.backup.lock',root/'cron/.tick.lock'):
            core.private_dir(path.parent)
            handle=stack.enter_context(path.open('a+b'))
            if not _lock(handle.fileno(),wait=False): raise RecoveryError('WRITERS_BUSY')
        _refuse_cron_writers(root)
        yield


def identify(root):
    from hermes_cli.gateway_runtime_discovery import query_identify
    return query_identify(root,timeout=2)


def _exact(identity,root):
    from .recovery_activation import _ready_identity
    if os.name=='nt':
        from .windows_native import canonical_directory
        root,_=canonical_directory(root)
    return _ready_identity(identity,root) and identity.get('supervisor') in ('none','manual') and bool(identity.get('instance_id')) and bool(identity.get('authority_epoch'))


def _pause_bound(root,identity):
    """Send the stock pause-for-update verb on a PID-authenticated connection.

    A second pathname-based query can hit a replacement owner. Authenticate the
    actual connected kernel peer BEFORE sending the destructive control verb.
    """
    from gateway.status import get_process_start_time
    pid,start=identity['pid'],identity['start_time']
    request=b'{"protocol":1,"verb":"pause-for-update","id":1}\n'
    if os.name=='nt':
        from . import windows_native as n
        from gateway import runtime_bootstrap_windows as wire
        from gateway.control_socket import windows_pipe_name
        process=n.api(n.K,'OpenProcess',n.W.HANDLE,n.W.DWORD,n.W.BOOL,n.W.DWORD)(0x1000|0x100000,False,pid)
        if not process: raise RecoveryError('OWNER_NOT_VERIFIED')
        handle=None
        try:
            if n.wait(process,0)!=258 or get_process_start_time(pid)!=start: raise RecoveryError('ROOT_EPOCH_CHANGED')
            native=wire._native()
            deadline=time.monotonic()+5
            handle=native.CreateFile(windows_pipe_name(root),0xC0000000,0,native.NULL,native.OPEN_EXISTING,
                                     native.FILE_FLAG_OVERLAPPED|wire._PIPE_CLIENT_SQOS,native.NULL)
            wire._peer_subject(handle,server=True)
            peer=n.W.ULONG()
            get=n.api(n.K,'GetNamedPipeServerProcessId',n.W.BOOL,n.W.HANDLE,n.C.POINTER(n.W.ULONG))
            if not get(int(handle),n.C.byref(peer)) or peer.value!=pid or n.wait(process,0)!=258:
                raise RecoveryError('ROOT_EPOCH_CHANGED')
            wire._write(handle,request,deadline)
            raw=wire._read_line(handle,deadline,512*1024)
        finally:
            if handle is not None: native.CloseHandle(handle)
            n.close(process)
    else:
        import select,socket,struct
        from hermes_cli.gateway_runtime_discovery import connect_private
        if not hasattr(os,'pidfd_open') or not hasattr(socket,'SO_PEERCRED'):
            raise RecoveryError('SCOPED_STOP_NOT_SUPPORTED')
        process=os.pidfd_open(pid)
        try:
            if select.select([process],[],[],0)[0] or get_process_start_time(pid)!=start: raise RecoveryError('ROOT_EPOCH_CHANGED')
            with connect_private(root,5) as peer:
                actual,uid,_=struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if actual!=pid or uid!=os.getuid() or select.select([process],[],[],0)[0]: raise RecoveryError('ROOT_EPOCH_CHANGED')
                peer.sendall(request)
                raw=bytearray()
                while b'\n' not in raw:
                    chunk=peer.recv(65536)
                    if not chunk or len(raw)+len(chunk)>512*1024: raise RecoveryError('DRAIN_NOT_ACKNOWLEDGED')
                    raw.extend(chunk)
        finally:os.close(process)
    reply=json.loads(bytes(raw).split(b'\n',1)[0])
    if type(reply) is not dict or reply.get('ok') is not True or reply.get('id')!=1 or reply.get('protocol')!=1:
        raise RecoveryError('DRAIN_NOT_ACKNOWLEDGED')
    return reply.get('result')


def drain(root, expected):
    """Protected SDK control only; no broad stop scan or signal fallback."""
    from gateway.control_socket import pause_gateway_for_update
    from gateway.status import get_process_start_time, _pid_exists
    current=identify(root)
    if current!=expected or not _exact(current,root): raise RecoveryError('ROOT_EPOCH_CHANGED')
    pid,start=current['pid'],current.get('start_time')
    if type(pid) is not int or start is None or get_process_start_time(pid)!=start: raise RecoveryError('OWNER_NOT_VERIFIED')
    ack=_pause_bound(root,current)
    if not ack or ack.get('pid')!=pid or not (ack.get('pausing') or ack.get('already_stopping')):
        raise RecoveryError('DRAIN_NOT_ACKNOWLEDGED')
    deadline=time.monotonic()+min(120,max(5,float(ack.get('drain_timeout',30))+5))
    while time.monotonic()<deadline:
        current_start=get_process_start_time(pid)
        if not _pid_exists(pid) or (current_start is not None and current_start!=start):
            from gateway.runtime_ownership import live_runtime_owner
            if live_runtime_owner(root) is None: return ack
        time.sleep(.05)
    raise RecoveryError('DRAIN_TIMEOUT')


class OwnedGateway:
    def __init__(self,root,*,retain=None,attempt_plan_digest=None):
        from hermes_cli.gateway_runtime_start import spawn_unmanaged_gateway
        from .recovery_activation import _WindowsLaunch
        self.root=cold._root(root)
        self.child=None;self.native=None;self._stopped=False
        self._never_spawned=True;self._ready_observed=False
        self.attempt_plan_digest=attempt_plan_digest
        if retain is not None: retain(self)
        # Only a preflight failure BEFORE invoking the SDK proves no spawn.
        # A producer exception without its returned Popen is ambiguous.
        log=root/'logs/gateway-stdio.log'
        if os.path.lexists(log):
            import stat
            with ExitStack() as guards:
                cold._retain(guards,log.parent,root)
                with core.regular_input(log) as fd:
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                        raise RecoveryError('TARGET_CONFLICT')
        self._never_spawned=False
        self.child=spawn_unmanaged_gateway(root,deadline=time.monotonic()+60)
        self.native=_WindowsLaunch(self.child) if os.name=='nt' else None
    def ready(self,timeout=60):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline and self.child.poll() is None:
            if self.native: self.native.capture()
            try: answer=identify(self.root)
            except (OSError,ValueError): answer={}
            pid=answer.get('pid')
            if _exact(answer,self.root) and (self.native.matches(pid) if self.native else pid==self.child.pid):
                self._ready_observed=True
                return answer
            time.sleep(.05)
        raise RecoveryError('START_NOT_READY')
    def cleanup(self):
        if self.child is None:
            if self._never_spawned: self._stopped=True;return
            raise RecoveryError('ATTEMPT_STOP_UNVERIFIED')
        if os.name=='nt' and self.native is None:
            raise RecoveryError('ATTEMPT_STOP_UNVERIFIED')
        if self.native: self.native.stop()
        if self.child.poll() is None:
            self.child.terminate()
            try: self.child.wait(timeout=10)
            except subprocess.TimeoutExpired: self.child.kill();self.child.wait(timeout=5)
        if self.native: self.native.close()
        self._stopped=self.child.poll() is not None


def _base(root):
    return root/'cache/finix/default-controller'


def _job(root,job):
    if not isinstance(job,str) or not re.fullmatch('[a-f0-9]{32}',job): raise RecoveryError('INVALID_JOB')
    return _base(root)/job


def _cache_code_matches(actual, expected):
    """Compare a bounded CPython marshal-v4/v5 code stream, without loading it.

    Only compile-produced immutable forms are recognized. References resolve to
    canonical integer IDs, never Python code objects. Sharing/intern flags are
    representation details: retaining an earlier compilation changes them even
    on identical source. Interning node IDs also bounds work on shared DAGs.
    """
    import sys
    if sys.implementation.name != 'cpython' or not (3, 11) <= sys.version_info[:2] <= (3, 14):
        return False
    if max(len(actual), len(expected)) > 16*1024*1024: return False
    nodes = {}
    def intern(key):
        if key not in nodes: nodes[key] = len(nodes)
        return nodes[key]
    def scan(data, budget):
        offset = 0; steps = 0; refs = []
        def take(size):
            nonlocal offset
            if size < 0 or size > len(data)-offset: raise ValueError()
            value = data[offset:offset+size]; offset += size
            return value
        def number(): return int.from_bytes(take(4), 'little', signed=True)
        def item(depth=0):
            nonlocal steps
            steps += 1
            if depth > 128 or steps > budget: raise ValueError()
            tag = take(1)[0]; kind = chr(tag & 127)
            if kind == 'r':
                if tag & 128: raise ValueError()
                index = number()
                if not 0 <= index < len(refs) or refs[index] is None: raise ValueError()
                return refs[index]
            slot = len(refs) if tag & 128 else None
            if slot is not None: refs.append(None)
            if kind in 'NFT.':
                if tag & 128: raise ValueError()
                key = (kind,)
            elif kind == 'i': key = (kind, take(4))
            elif kind == 'l':
                size = number(); key = (kind, size, take(abs(size)*2))
            elif kind in 'gy': key = (kind, take(8 if kind == 'g' else 16))
            elif kind == 's': key = (kind, take(number()))
            elif kind in 'utaAzZ':
                raw = take(take(1)[0] if kind in 'zZ' else number())
                key = ('text', raw.decode('ascii' if kind in 'aAzZ' else 'utf8', errors='surrogatepass'))
            elif kind in '()>':
                size = take(1)[0] if kind == ')' else number()
                if size < 0 or size > budget-steps: raise ValueError()
                children = tuple(item(depth+1) for _ in range(size))
                if kind == '>':
                    unique = frozenset(children)
                    if len(unique) != size: raise ValueError()
                    key = ('set', unique)
                else: key = ('tuple', children)
            elif kind == 'c':
                header = take(20)  # 3.11-3.14: five fixed-width compiler fields.
                fields = tuple(item(depth+1) for _ in range(8))
                line = take(4)
                tail = (item(depth+1), item(depth+1))
                key = ('code', header, fields, line, tail)
            elif kind == ':' and sys.version_info[:2] >= (3, 14):
                key = ('slice', tuple(item(depth+1) for _ in range(3)))
            else: raise ValueError()
            result = intern(key)
            if slot is not None: refs[slot] = result
            return result
        result = item()
        if offset != len(data): raise ValueError()
        return result, steps
    try:
        trusted, count = scan(expected, len(expected))
        candidate, _ = scan(actual, count*2+128)
        return candidate == trusted
    except (ValueError, UnicodeError, IndexError, RecursionError):
        return False


def _source_cache_matches(path, installed, source_pins):
    """Admit only bounded source-equivalent conventional timestamp caches."""
    import marshal
    import stat
    import struct
    import sys
    def admitted(fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: return False
        if os.name == 'nt':
            import msvcrt
            from .windows_native import trusted_handle
            return trusted_handle(msvcrt.get_osfhandle(fd))
        return info.st_uid == os.geteuid() and not info.st_mode & 0o022
    try:
        source = Path(importlib.util.source_from_cache(str(path)))
        relative = source.relative_to(installed).as_posix()
        if (relative not in source_pins or source.suffix != '.py'
                or Path(importlib.util.cache_from_source(str(source))) != path): return False
        with ExitStack() as guards:
            if os.name == 'nt':
                from . import windows_native as win
                anchor = guards.enter_context(win.directory(path.parent))
                if not all(win.trusted_handle(h, ancestor=True) for h in anchor.handles): return False
            else:
                from .recovery import _PosixAnchor
                _PosixAnchor(guards, path.parent, owned_from=installed)
            with core.regular_input(source) as fd:
                if not admitted(fd): return False
                info = os.fstat(fd); data = core.read_regular(fd, 4*1024*1024)
            if hashlib.sha256(data).hexdigest() != source_pins[relative]: return False
            with core.regular_input(path) as fd:
                if not admitted(fd): return False
                cached = core.read_regular(fd, 16*1024*1024)
        header = (importlib.util.MAGIC_NUMBER + b'\x00'*4
                  + struct.pack('<II', int(info.st_mtime)&0xffffffff, len(data)&0xffffffff))
        if cached[:16] != header: return False
        compiled = compile(data, str(source), 'exec', dont_inherit=True, optimize=sys.flags.optimize)
        # Serialize trusted compiler output only. Never pass cached bytes to
        # marshal.load(s), exec, CodeType, or an import loader.
        expected = marshal.dumps(compiled)
        return _cache_code_matches(cached[16:], expected)
    except Exception:
        return False


def _current_finix(root):
    """Digest only the currently loaded, already enabled control-plane package."""
    if _config(root).get('plugins',{}).get('enabled')!=['finix']:
        raise RecoveryError('CURRENT_FINIX_NOT_ADMITTED')
    from .recovery_activation import profile_digest
    installed=root/'plugins/finix'
    current=Path(__file__).resolve().parent.parent
    def inventory(base):
        # The shipped dashboard manifest names this exact SDK bootstrap adapter.
        # Pin its bytes like the other CURRENT code; no archived plugin gains trust.
        names=['__init__.py','plugin.yaml','dashboard/plugin_api.py']+['finix/'+p.name for p in sorted((base/'finix').glob('*.py'))]
        return {name:core.digest_file(base/name) for name in names}
    actual=inventory(installed)
    if actual!=inventory(current): raise RecoveryError('CURRENT_FINIX_CHANGED')
    # Reject all extra executable files in the installed trusted package.
    for path in installed.rglob('*'):
        # rglob deliberately does not descend into POSIX directory symlinks.
        if path.name == '__pycache__' and path.is_symlink():
            raise RecoveryError('CURRENT_FINIX_CHANGED')
        if path.is_file() and path.suffix in ('.py','.pyc','.pyd','.so','.dll') and path.relative_to(installed).as_posix() not in actual:
            if path.suffix == '.pyc' and _source_cache_matches(path, installed, actual): continue
            raise RecoveryError('CURRENT_FINIX_CHANGED')
    return actual


def prepare_live(cap,root,*,journal_root,intent):
    """Server-only narrow exemption; archived plugins stay parked/disabled.

    Applies only to the verified private copy BEFORE the cold plan is sealed.
    No cold-engine API or published profile configuration is overwritten here.
    """
    root=cold._root(root)
    if cap not in cold._CAPABILITIES: raise RecoveryError('INVALID_CAPABILITY')
    trusted=_current_finix(root)
    preflight(root,trusted_finix=True)
    binding=cold._CAPABILITIES[cap]
    source=binding['source']
    from .recovery_activation import profile_digest
    if profile_digest(source)!=binding['digest']: raise RecoveryError('SOURCE_CHANGED')
    config=_config(source)
    config.setdefault('plugins',{})['enabled']=['finix']
    config.setdefault('gateway',{}).update(standalone=True,multiplex_profiles=False)
    if not _inert(config,True): raise RecoveryError('DEFAULT_AUTORUN_NOT_SUPPORTED')
    core.atomic_json(source/'config.yaml',config)
    binding['digest']=profile_digest(source)
    engine=cold.prepare(cap,root,journal_root=journal_root,intent=intent,
                        runtime_attempt_policy='runtime-attempt-preserve-v1')
    cold._write(engine.directory/'current-finix.json',{'digest':trusted,'plan_digest':engine.plan_digest})
    return engine


def _verify_engine(engine):
    plan=engine._plan()
    source=Path(plan['source'])
    from .recovery_activation import profile_digest
    if cold._identity(source)!=plan['source_identity'] or profile_digest(source)!=plan['source_digest']:
        raise RecoveryError('SOURCE_CHANGED')
    for name,digest in plan['entries'].items():
        if core.digest_file(engine.directory/'payload'/name)!=digest: raise RecoveryError('PAYLOAD_CHANGED')


def _read_status(directory):
    # Keep the native non-delete-sharing reader, but never overlap it with our
    # own atomic writer. This is a short state lock, not the controller lease.
    from .backup_jobs import state_lease
    with state_lease(directory/'status.lock'):
        return cold._load(directory/'status.json')


def _write_status(directory,value):
    from .backup_jobs import state_lease
    with state_lease(directory/'status.lock'):
        cold._write(directory/'status.json',value)


class ColdController:
    """Server-only registration and detached ACK; status does not lock a worker."""
    def __init__(self,root):
        self.root=cold._root(root)
    def confirmation(self,engine):
        if type(engine) is not cold.DefaultRecovery: raise RecoveryError('INVALID_CAPABILITY')
        if Path(engine._plan()['root'])!=self.root: raise RecoveryError('WRONG_DEFAULT_ROOT')
        owner=identify(self.root)
        if not _exact(owner,self.root): raise RecoveryError('OWNER_NOT_VERIFIED')
        return {'consent':engine.consent(),'runtime':{key:owner[key] for key in
                ('instance_id','authority_epoch','pid','start_time','hermes_home')}}
    def register(self,engine,confirmation):
        if confirmation!=self.confirmation(engine): raise RecoveryError('CONSENT_REQUIRED')
        consent=confirmation['consent']
        plan=engine._plan()
        _verify_engine(engine)
        if engine.status()['state']!='prepared': raise RecoveryError('JOB_NOT_PREPARED')
        if Path(plan['root'])!=self.root: raise RecoveryError('WRONG_DEFAULT_ROOT')
        exemption=engine.directory/'current-finix.json'
        trusted=cold._load(exemption) if exemption.exists() else None
        if trusted is not None and trusted!={'digest':_current_finix(self.root),'plan_digest':engine.plan_digest}:
            raise RecoveryError('CURRENT_FINIX_CHANGED')
        snapshot=preflight(self.root,trusted_finix=trusted is not None)
        with writer_fence(self.root): pass
        owner=identify(self.root)
        if not _exact(owner,self.root): raise RecoveryError('OWNER_NOT_VERIFIED')
        if any(owner.get(key)!=value for key,value in confirmation['runtime'].items()): raise RecoveryError('ROOT_EPOCH_CHANGED')
        job=uuid.uuid4().hex
        directory=core.private_dir(_job(self.root,job))
        record={'version':1,'job':job,'engine':plan['id'],'plan_digest':engine.plan_digest,
                'consent':consent,'owner':owner,'snapshot':snapshot,'trusted_finix':trusted,'root_identity':cold._identity(self.root)}
        cold._write(directory/'record.json',record)
        _write_status(directory,{'state':'registered','error':None})
        return job
    def status(self,job):
        return _read_status(_job(self.root,job))
    def launch(self,job):
        from .backup_jobs import file_lease
        directory=_job(self.root,job)
        with file_lease(directory/'dispatch.lock'):
            state=self.status(job)['state']
            if state!='registered': return {'job':job,'state':state}
            return self._dispatch(job)
    def reopen(self,job):
        """Detached continuation after controller death; never adopt a live owner."""
        from .backup_jobs import file_lease
        from gateway.runtime_ownership import live_runtime_owner
        directory=_job(self.root,job)
        with file_lease(directory/'dispatch.lock'):
            with file_lease(directory/'controller.lock'):
                status=self.status(job)
                if status['state'] in ('rollback_not_ready','manual_required','reconciliation_required'):
                    return {'job':job,'state':status['state']}
                if status['state'] in ('ready','rolled_back_ready'):
                    if identify(self.root)!=status.get('identity'): raise RecoveryError('ROOT_EPOCH_CHANGED')
                    return {'job':job,'state':status['state']}
                if live_runtime_owner(self.root) is not None: raise RecoveryError('REOPEN_LIVE_OWNER_UNVERIFIED')
            return self._dispatch(job)
    def _dispatch(self,job):
        directory=_job(self.root,job)
        record=cold._load(directory/'record.json')
        if record['job']!=job: raise RecoveryError('INVALID_JOB')
        dispatch_nonce=uuid.uuid4().hex
        cold._write(directory/'dispatch.json',{'nonce':dispatch_nonce,'record_digest':cold._digest(record)})
        # Freeze the current package outside all replaced members. No archive
        # content, arbitrary client path, or ambient PYTHONPATH enters dispatch.
        package=core.private_dir(directory/'package/finix')
        source=Path(__file__).parent
        def pin_copy(origin,target):
            if target.exists():
                if core.digest_file(origin)!=core.digest_file(target): raise RecoveryError('PINNED_PACKAGE_CHANGED')
            else: cold._copy(origin,target)
        for path in sorted(source.glob('*.py')):
            pin_copy(path,package/path.name)
        for name in ('__init__.py','plugin.yaml'):
            pin_copy(source.parent/name,package.parent/name)
        worker=package/'default_recovery_worker.py'
        from .workspace_drive import trusted_runtime
        import hermes_constants
        sdk=Path(hermes_constants.__file__).resolve().parent
        env=dict(os.environ)
        env.pop('PYTHONPATH',None)
        env.pop('HERMES_PROFILE',None)
        env.update(HERMES_HOME=str(self.root))
        kwargs={'start_new_session':True} if os.name!='nt' else {'creationflags':subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP}
        with trusted_runtime(worker,entry=worker) as (prefix,entry):
            with (directory/'worker.log').open('ab') as log:
                child=subprocess.Popen([*prefix,entry,str(sdk),job],cwd=sdk,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,**kwargs)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            status=self.status(job)
            if status['state']=='acknowledged' and status.get('dispatch_nonce')==dispatch_nonce:
                cold._write(directory/'release.json',{'record_digest':cold._digest(record),'dispatch_nonce':dispatch_nonce})
                return {'job':job,'worker_pid':child.pid,'state':'acknowledged'}
            if status['state'] in ('ready','rolled_back_ready','manual_required','reconciliation_required','rollback_not_ready','refused'):
                return {'job':job,'worker_pid':child.pid,'state':status['state']}
            if child.poll() is not None: break
            time.sleep(.02)
        raise RecoveryError('WORKER_NOT_ACKNOWLEDGED')


def run_job(root,job):
    """Detached process entry, fenced independently from source HTTP lifetime."""
    from .backup_jobs import file_lease
    root=cold._root(root)
    directory=_job(root,job)
    with file_lease(directory/'controller.lock'):
        record=cold._load(directory/'record.json')
        if record.get('job')!=job or record.get('root_identity')!=cold._identity(root): raise RecoveryError('ROOT_CHANGED')
        engine=cold.DefaultRecovery(root/'cache/finix/default-recovery'/record['engine'],record['plan_digest'])
        if record['consent']!=engine.consent(): raise RecoveryError('CONSENT_REQUIRED')
        def state(value,error=None,**extra):
            event=dict(state=value,error=error,**extra)
            history=directory/'history.json'
            events=cold._load(history) if history.exists() else []
            events.append(dict(event,worker_pid=os.getpid()))
            cold._write(history,events)
            _write_status(directory,event)
        dispatch=cold._load(directory/'dispatch.json') if (directory/'dispatch.json').exists() else None
        if dispatch is not None and (set(dispatch)!={'nonce','record_digest'}
            or not cold._hex(dispatch['nonce'],32) or dispatch['record_digest']!=cold._digest(record)):
            raise RecoveryError('DISPATCH_NOT_VERIFIED')
        if engine._plan()['version']==4 and dispatch is None: raise RecoveryError('DISPATCH_NOT_VERIFIED')
        def acknowledge():
            extra={'dispatch_nonce':dispatch['nonce']} if dispatch else {}
            state('acknowledged',worker_pid=os.getpid(),**extra)
            expected={'record_digest':cold._digest(record),**extra}
            deadline=time.monotonic()+30
            while True:
                if (directory/'release.json').exists() and cold._load(directory/'release.json')==expected:return
                if time.monotonic()>deadline:raise RecoveryError('ACK_NOT_RELEASED')
                time.sleep(.02)
        previous_status=_read_status(directory)
        previous=previous_status['state']
        if (engine.directory/'success-commit.json').exists() or (engine.directory/'ready-observed.json').exists():
            if previous!='ready': state('reconciliation_required','SUCCESS_RECONCILIATION_REQUIRED')
            return
        if previous=='rollback_not_ready':
            # Terminal manual intervention: never retry mutation or start over
            # a family whose rollback/start could not be proved safe.
            raise RecoveryError(previous_status.get('error') or 'ROLLBACK_FAILED')
        if previous in ('ready','rolled_back_ready'):
            if identify(root)!=previous_status.get('identity'): raise RecoveryError('ROOT_EPOCH_CHANGED')
            return
        if previous!='registered':
            # Never adopt a runtime after losing the retained Popen/handle.
            from gateway.runtime_ownership import live_runtime_owner
            if live_runtime_owner(root) is not None: raise RecoveryError('REOPEN_LIVE_OWNER_UNVERIFIED')
            if engine._plan()['version']==4 and (engine.directory/'attempt-start.json').exists():
                custody=Path(engine._plan()['runtime_attempt']['custody'])
                if not (custody/'execution-manifest.json').exists():
                    state('manual_required','ATTEMPT_STOP_UNVERIFIED')
                    return
            acknowledge()
            try:
                with writer_fence(root):
                    if engine.status()['state']!='prepared' and not (engine.directory/'attempt-start.json').exists():
                        engine.rollback(record['consent'])
                if engine._plan()['version']==4 and (engine.directory/'attempt-start.json').exists():
                    engine.rollback_attempt(record['consent'],None)
            except Exception as rollback_failure:
                error=rollback_failure.code if isinstance(rollback_failure,RecoveryError) else 'ROLLBACK_FAILED'
                state('rollback_not_ready',error)
                raise
            trusted=record.get('trusted_finix')
            if trusted is not None and trusted!={'digest':_current_finix(root),'plan_digest':engine.plan_digest}:
                raise RecoveryError('CURRENT_FINIX_CHANGED')
            preflight(root,trusted_finix=trusted is not None)
            old=OwnedGateway(root)
            try: ready=old.ready()
            except BaseException: old.cleanup();raise
            state('rolled_back_ready',identity=ready,launcher_pid=old.child.pid,
                  owned_runtime_pids=sorted(old.native.runtime_pids) if old.native else [old.child.pid])
            return
        acknowledge()
        trusted=record.get('trusted_finix')
        if trusted is not None and trusted!={'digest':_current_finix(root),'plan_digest':engine.plan_digest}:
            raise RecoveryError('CURRENT_FINIX_CHANGED')
        if preflight(root,trusted_finix=trusted is not None)!=record['snapshot']: raise RecoveryError('AUTORUN_CHANGED')
        with writer_fence(root): pass
        _verify_engine(engine)
        if identify(root)!=record['owner']: raise RecoveryError('ROOT_EPOCH_CHANGED')
        state('draining')
        drain(root,record['owner'])
        state('stopped')
        new=None;attempts=[]
        try:
            with writer_fence(root):
                if preflight(root,trusted_finix=trusted is not None)!=record['snapshot']: raise RecoveryError('AUTORUN_CHANGED')
                state('publishing')
                engine.apply(record['consent'])
            state('published_offline')
            if engine._plan()['version']==4:
                cold._write(engine.directory/'attempt-start.json',{'version':1,'plan_digest':engine.plan_digest})
            new=OwnedGateway(root,retain=attempts.append,attempt_plan_digest=engine.plan_digest)
            ready=new.ready()
        except BaseException as failure:
            cause=failure.code if isinstance(failure,RecoveryError) else type(failure).__name__
            if new is None and attempts: new=attempts[0]
            proof=None
            if new:
                try:
                    new.cleanup()
                    if engine._plan()['version']==4:
                        from .runtime_attempt import stopped_attempt
                        proof=stopped_attempt(engine,new)
                except BaseException:
                    state('manual_required','ATTEMPT_STOP_UNVERIFIED');raise
            state('rolling_back',error=cause)
            try:
                with writer_fence(root):
                    if engine.status()['state']!='prepared' and not (engine._plan()['version']==4 and new is not None):
                        engine.rollback(record['consent'])
                if engine._plan()['version']==4 and new is not None:
                    engine.rollback_attempt(record['consent'],proof)
            except Exception as rollback_failure:
                error=rollback_failure.code if isinstance(rollback_failure,RecoveryError) else 'ROLLBACK_FAILED'
                state('rollback_not_ready',error)
                raise
            preflight(root,trusted_finix=trusted is not None)
            old=OwnedGateway(root)
            try: ready=old.ready()
            except BaseException:
                old.cleanup();state('rollback_not_ready','OLD_START_FAILED');raise
            state('rolled_back_ready',error='NEW_START_FAILED',identity=ready,launcher_pid=old.child.pid,
                  owned_runtime_pids=sorted(old.native.runtime_pids) if old.native else [old.child.pid])
            return
        # Live READY ends the launch-failure rollback boundary even when saving
        # its durable success record fails. Never stop/rollback in this except.
        try:
            if engine._plan()['version']==4:
                from .runtime_attempt import commit_success
                commit_success(engine,ready)
            state('ready',identity=ready,launcher_pid=new.child.pid,
                  owned_runtime_pids=sorted(new.native.runtime_pids) if new.native else [new.child.pid])
        except BaseException:
            state('reconciliation_required','SUCCESS_RECONCILIATION_REQUIRED')
            raise
