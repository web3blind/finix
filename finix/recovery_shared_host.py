"""Attested existing root host; never an owned process or a stop capability."""
import json
import os
from pathlib import Path
import time
from .recovery_import import RecoveryImportError


def _fail():
    raise RecoveryImportError('SERVING_NOT_CONFIRMED')


def _bound(root, identity, verb, params=None, *, before_write=None):
    """SDK protocol on the SAME kernel-authenticated peer before any write.

    Compose the reviewed native ownership transport, not the diagnostic SDK
    pathname wrapper (which cannot bind its write to an expected PID generation).
    """
    from gateway.status import get_process_start_time
    pid, start = identity['pid'], identity['start_time']
    request = json.dumps(dict(protocol=1, id=1, verb=verb, **({'params':params} if params else {}))).encode()+b'\n'
    if get_process_start_time(pid) != start:
        _fail()
    if os.name == 'nt':
        from . import windows_native as n
        from gateway import runtime_bootstrap_windows as wire
        from gateway.control_socket import windows_pipe_name
        process=n.api(n.K,'OpenProcess',n.W.HANDLE,n.W.DWORD,n.W.BOOL,n.W.DWORD)(0x1000|0x100000,False,pid)
        if not process:_fail()
        handle=None
        try:
            if n.wait(process,0)!=258 or get_process_start_time(pid)!=start:_fail()
            native=wire._native();deadline=time.monotonic()+8
            handle=native.CreateFile(windows_pipe_name(root),0xC0000000,0,native.NULL,native.OPEN_EXISTING,
                                    native.FILE_FLAG_OVERLAPPED|wire._PIPE_CLIENT_SQOS,native.NULL)
            wire._peer_subject(handle,server=True)
            peer=n.W.ULONG()
            get=n.api(n.K,'GetNamedPipeServerProcessId',n.W.BOOL,n.W.HANDLE,n.C.POINTER(n.W.ULONG))
            if not get(int(handle),n.C.byref(peer)) or peer.value!=pid or n.wait(process,0)!=258:_fail()
            if before_write is not None:before_write()
            if n.wait(process,0)!=258 or get_process_start_time(pid)!=start:_fail()
            wire._write(handle,request,deadline)
            raw=wire._read_line(handle,deadline,512*1024)
            if n.wait(process,0)!=258 or get_process_start_time(pid)!=start:_fail()
        finally:
            if handle is not None:native.CloseHandle(handle)
            n.close(process)
    else:
        import select,socket,struct
        from hermes_cli.gateway_runtime_discovery import connect_private
        if not hasattr(os,'pidfd_open') or not hasattr(socket,'SO_PEERCRED'):_fail()
        process=os.pidfd_open(pid)
        try:
            if select.select([process],[],[],0)[0] or get_process_start_time(pid)!=start:_fail()
            with connect_private(root,8) as peer:
                actual,uid,_=struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if actual!=pid or uid!=os.getuid() or select.select([process],[],[],0)[0]:_fail()
                if before_write is not None:before_write()
                if select.select([process],[],[],0)[0] or get_process_start_time(pid)!=start:_fail()
                peer.sendall(request);raw=bytearray()
                while b'\n' not in raw:
                    chunk=peer.recv(65536)
                    if not chunk or len(raw)+len(chunk)>512*1024:_fail()
                    raw.extend(chunk)
                if select.select([process],[],[],0)[0] or get_process_start_time(pid)!=start:_fail()
        finally:os.close(process)
    reply=json.loads(bytes(raw).split(b'\n',1)[0])
    if type(reply) is not dict or reply.get('ok') is not True or reply.get('id')!=1 or reply.get('protocol')!=1:_fail()
    result=reply.get('result')
    if type(result) is not dict:_fail()
    return result


def _roster(identity):
    roster=identity.get('served_profiles')
    if not isinstance(roster,list) or not roster:_fail()
    result={}
    for entry in roster:
        if type(entry) is not dict or set(entry)!={'home','profile_id'} or entry['home']!=entry['profile_id'] or not isinstance(entry['home'],str):_fail()
        if entry['home'] in result:_fail()
        result[entry['home']]=entry
    return result


class SharedTargetRuntime:
    """Only exact target serve authority; failure preserves partial shared state."""
    def __init__(self,record,identity):
        self.record=record
        self.identity=identity
        self.original=_roster(identity)

    def check(self):
        from .recovery_pipeline import _identity, _CAPS, _admit
        r=self.record
        cap=next((cap for cap,registered in _CAPS.items() if registered is r),None)
        if cap is None:_fail()
        _admit(cap)
        r['service']._fence(r['profile_id'],r['activation'])
        if (_identity(r['root'])!=r['root_identity'] or _identity(r['target'])!=r['native']
                or _identity(r['service'].profile)!=r['source_native']):_fail()
        current=_bound(r['root'],self.identity,'identify')
        if (current.get('state')!='ready' or current.get('pending') or current.get('error')
                or any(current.get(k)!=self.identity.get(k) for k in ('pid','start_time','instance_id','authority_epoch','hermes_home'))):_fail()
        roster=_roster(current)
        if any(roster.get(k)!=v for k,v in self.original.items()):_fail()
        # No unrequested sibling admission is attributed to this operation.
        if set(roster)-set(self.original)-{str(r['target'])}:_fail()
        _admit(cap)
        if _identity(r['service'].profile)!=r['source_native']:_fail()
        return current

    def ensure(self):
        from hermes_constants import profile_name_for_home
        r=self.record
        self.check()
        if str(r['target']) in self.original:_fail()
        expected={profile_name_for_home(Path(home)) for home in self.original}|{r['name']}
        # The SDK accepts ONE request per connection, no conditional fields.
        # Re-query checks just before writing on the retained authenticated peer.
        # This is boundary admission, NOT atomic protection against same-UID
        # TARGET replacement between this check and SDK name resolution.
        def before_write():
            if str(r['target']) in _roster(self.check()):_fail()
        ack=_bound(r['root'],self.identity,'serve-profile',{'name':r['name']},before_write=before_write)
        names=ack.get('served_profiles')
        if (ack.get('served')!=r['name'] or ack.get('pending') or ack.get('error')
                or not isinstance(names,list) or any(not isinstance(n,str) for n in names)
                or len(names)!=len(expected) or set(names)!=expected):
            # A non-final ACK cannot authorize durable unparked startup. Restore
            # the inert marker only after re-admitting the retained exact target.
            # This does not stop or claim to revoke an ambiguously served host.
            from . import core
            from .recovery_pipeline import _CAPS, _admit
            cap=next((cap for cap,record in _CAPS.items() if record is r),None)
            if cap is not None:
                _admit(cap)
                with core.regular_output(r['target']/'gateway.parked') as fd:
                    os.write(fd,b'finix-recovered-review-required\n')
                    os.fsync(fd)
            _fail()
        r['serve_ack']=ack
        self.ready()
        return ack

    def ready(self,timeout=60):
        current=self.check()
        target=str(self.record['target'])
        if _roster(current).get(target)!={'home':target,'profile_id':target}:_fail()
        return current

    def endpoint(self,endpoint):
        current=self.ready()
        if (endpoint is None or endpoint.profile_id!=str(self.record['target'])
                or endpoint.instance_id!=current['instance_id']
                or endpoint.api_origin!=current['api_origin']):_fail()
        return endpoint


def shared_host(record):
    from .recovery_pipeline import _identity
    from gateway import host_rendezvous as hr
    from hermes_cli.gateway_runtime_discovery import query_identify, missing_owner_state
    from gateway.status import get_process_start_time
    # Map ONLY explicit trusted native homes before reading discovery metadata.
    # The rendezvous is an exclusion hint, never a destination or fallback.
    root,source=Path(record['root']),Path(record['service'].profile)
    try:
        homes=list(dict.fromkeys((root,source)))
        native={home:_identity(home) for home in homes}
        owner=hr.read_record(hr.ROLE_GATEWAY)
        if owner is not None:
            if getattr(owner,'home',None) not in {str(home) for home in homes}:_fail()
            if not hr.record_names_live_owner(owner):_fail()
        for home in homes:
            if _identity(home)!=native[home]:_fail()
            try:
                identity=query_identify(home,timeout=2)
            except FileNotFoundError:
                if (owner is not None and owner.home==str(home)) or missing_owner_state(home)!='absent':_fail()
                continue
            pid=identity.get('pid')
            if (type(pid) is not int or pid<=0 or identity.get('hermes_home')!=str(home)
                    or identity.get('state')!='ready' or identity.get('pending') or identity.get('error')
                    or not identity.get('instance_id') or not identity.get('authority_epoch')
                    or identity.get('start_time') is None
                    or get_process_start_time(pid)!=identity['start_time']):_fail()
            if owner is not None and owner.home==str(home) and owner.pid!=pid:_fail()
            # Re-attest on the kernel PID/generation-bound protected channel.
            current=_bound(home,identity,'identify')
            if (any(current.get(k)!=identity.get(k) for k in ('pid','start_time','instance_id','authority_epoch','hermes_home','state','served_profiles'))
                    or current.get('pending') or current.get('error') or _identity(home)!=native[home]):_fail()
            roster=_roster(identity)
            if str(home) not in roster:_fail()
            if home==root and len(roster)>1:
                if str(source) not in roster:_fail()
                if 'target' in record and str(record['target']) in roster:_fail()
                if record.get('root_identity',native[root])!=native[root]:_fail()
                if any(_identity(h)!=native[h] for h in homes):_fail()
                record['source_native']=native[source]
                return SharedTargetRuntime(record,identity)
            if set(roster)!={str(home)}:_fail()
        if any(_identity(home)!=native[home] for home in homes):_fail()
        return None
    except RecoveryImportError:
        raise
    except Exception:
        # SDK transport diagnostics can contain private paths; never export them.
        _fail()


def check_bootstrap_compatibility(service):
    """Read-only early geometry check; never recreates historical authority.

    Runtime/native admission is repeated at bootstrap. Metadata communicates
    the cooperative condition; it never grants server mutation authority.
    """
    from hermes_cli import profiles as host
    from .recovery_import import verify_host_compatibility
    verify_host_compatibility(host)
    from hermes_constants import get_default_hermes_root
    runtime=shared_host({'root':Path(get_default_hermes_root()),'service':service})
    return ({'contract':'cooperative-root-boundaries-v1',
             'condition':'no-concurrent-same-user-target-replacement',
             'atomic_target_binding':False} if runtime is not None else None)
