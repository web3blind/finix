"""Confirmed serving of a job-bound inert import through pinned SDK lifecycle.

Prefer exact hot-serve ACK; a single-profile host gets only a new standalone
recovered child with exact ready identity/PID. Never rescan or restart the host.
"""
from contextlib import ExitStack, nullcontext
import hashlib
import importlib.util
import os
from pathlib import Path

from . import core
from .recovery_import import RecoveryImportError, _members, verify_host_compatibility

# Normalized bytes of actual installed 0.23.0 Desktop host, not a git checkout.
LIFECYCLE_PINS = {
    'hermes_cli.gateway_runtime_start':'30c0a8fcbb2ebadd119ddbf54f21712818a2f68a26125d64eddbdb6916df2f15',
    'hermes_cli.gateway_runtime_discovery':'c8e3d10f25f3f2ffcf7ba1fc249f2decc3bc69340c29cbcec5036dff9cf18256',
    'gateway.control_socket':'5216bac6e8f763285c1f6d5776e3764fb1e6a07f6980f4aa869b993faed199b6',
    'gateway.host_attach':'a660ed4a85efc3c827816c5390d9e13eb13ff9492ae60d36476501c7f02bfa38',
    'gateway.run_profile_reconcile':'808de89a5884f285234c0ad7d2209754aedf4e749023323daca5afc08ea8d3b1',
    'hermes_cli.gateway_profile_lifecycle':'6b6ac8b94a05561280966107eb5cb35168e70b19fd2cb46766a906890587d8c6',
}


def profile_digest(target, retained=None):
    with (ExitStack() if retained is None else nullcontext(retained)) as stack:
        result={}
        from .preview_budget import check
        for name,path in _members(target,stack):
            check()
            with core.regular_input(path) as fd, os.fdopen(os.dup(fd),'rb') as stream:
                digest=hashlib.sha256()
                while chunk:=stream.read(1024*1024):
                    check()
                    digest.update(chunk)
                result[name]=digest.hexdigest()
        return result


class _WindowsLaunch:
    """Retain the launch and its direct native Python redirector child.

    Same SID is not an authorization boundary: ancestry to the retained Popen,
    native creation ordering, executable identity and authenticated SDK home are
    all required. Never adopt a matching-home process discovered elsewhere.
    """
    def __init__(self, child):
        from . import windows_native as n
        self.n, self.child, self.handles = n, child, {}
        self.launch_time = self._time(int(child._handle))
        self.runtime_pids = set()

    def _time(self, handle):
        n = self.n
        times = [n.W.FILETIME() for _ in range(4)]
        get = n.api(n.K, 'GetProcessTimes', n.W.BOOL, n.W.HANDLE,
                    *([n.C.POINTER(n.W.FILETIME)] * 4))
        if not get(handle, *[n.C.byref(t) for t in times]):
            raise OSError('Cannot inspect owned process creation')
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def _sid(self, handle):
        n = self.n
        token, size = n.W.HANDLE(), n.W.DWORD()
        open_token = n.api(n.A, 'OpenProcessToken', n.W.BOOL, n.W.HANDLE,
                           n.W.DWORD, n.C.POINTER(n.W.HANDLE))
        get = n.api(n.A, 'GetTokenInformation', n.W.BOOL, n.W.HANDLE,
                    n.C.c_int, n.C.c_void_p, n.W.DWORD, n.C.POINTER(n.W.DWORD))
        if not open_token(handle, 8, n.C.byref(token)):
            raise OSError('Cannot inspect owned process principal')
        try:
            get(token, 1, None, 0, n.C.byref(size))
            buf = n.C.create_string_buffer(size.value)
            if not get(token, 1, buf, size, n.C.byref(size)):
                raise OSError('Cannot inspect owned process principal')
            return n.sid_text(n.C.cast(buf, n.C.POINTER(n.C.c_void_p))[0])
        finally:
            n.close(token)

    def capture(self):
        import psutil
        import sys
        n = self.n
        # Only direct children of our still-live retained launcher. The Windows
        # venv redirector waits for this Python child; no arbitrary tree adoption.
        if self.child.poll() is not None:
            return
        parent = psutil.Process(self.child.pid)
        for process in parent.children(recursive=False):
            if process.pid in self.handles:
                continue
            handle = n.api(n.K, 'OpenProcess', n.W.HANDLE, n.W.DWORD,
                           n.W.BOOL, n.W.DWORD)(0x100001 | 0x1000, False, process.pid)
            if not handle:
                continue
            try:
                created = self._time(handle)
                if (process.ppid() != self.child.pid or created < self.launch_time
                        or self.child.poll() is not None
                        or not os.path.samefile(process.exe(), sys._base_executable)
                        or self._sid(handle) != n.current_sid()
                        or n.wait(handle, 0) != 258):
                    continue
                self.handles[process.pid] = (handle, created)
                self.runtime_pids.add(process.pid)
                handle = None
            except (OSError, psutil.Error):
                continue
            finally:
                if handle:
                    n.close(handle)

    def matches(self, pid):
        if type(pid) is not int or self.child.poll() is not None:
            return False
        if pid == self.child.pid:
            return True
        self.capture()
        entry = self.handles.get(pid)
        return bool(entry and self.n.wait(entry[0], 0) == 258
                    and self._time(entry[0]) == entry[1])

    def stop(self):
        self.capture()
        n = self.n
        terminate = n.api(n.K, 'TerminateProcess', n.W.BOOL, n.W.HANDLE, n.W.UINT)
        for handle, _ in self.handles.values():
            if n.wait(handle, 0) == 258:
                if not terminate(handle, 1) or n.wait(handle, 10000) != 0:
                    raise OSError('Cannot reap owned runtime')

    def close(self):
        for handle, _ in self.handles.values():
            self.n.close(handle)
        self.handles.clear()

def _ready_identity(identity, target):
    return (isinstance(identity, dict) and identity.get('state') == 'ready'
            and not identity.get('pending') and not identity.get('error')
            and identity.get('hermes_home') == str(target)
            and identity.get('served_profiles') == [
                {'home': str(target), 'profile_id': str(target)}])


def _start_standalone(target):
    """Launch only a new inert home; require its exact live SDK identity and PID.

    No global rescan, service-manager operation, replacement, or active selector.
    """
    import time
    import subprocess
    from .recovery_import import _read, _yaml_mapping, MAX_CONFIG
    from hermes_cli.gateway_runtime_start import spawn_unmanaged_gateway
    from hermes_cli.gateway_runtime_discovery import query_identify
    if os.name == 'nt':
        # SDK spawn resolves the native on-disk spelling (including ancestors).
        # Canonicalize through the no-reparse identity verifier, never lowercase
        # an untrusted identify home or loosen exact roster matching.
        from .windows_native import canonical_directory
        target, _ = canonical_directory(target)
    config_path = target / 'config.yaml'
    config = _yaml_mapping(_read(config_path, MAX_CONFIG))
    config.setdefault('gateway', {}).update(standalone=True, multiplex_profiles=False)
    # JSON is valid YAML. Use the existing private atomic writer, never follow
    # a config symlink or change any sibling/default configuration.
    with core.locked(target):
        core.atomic_json(config_path, config)
    child = None
    owned = None
    deadline = time.monotonic() + 60
    try:
        child = spawn_unmanaged_gateway(target, deadline=deadline)
        if os.name == 'nt':
            owned = _WindowsLaunch(child)
        while time.monotonic() < deadline:
            if owned is not None:
                owned.capture()
            if child.poll() is not None:
                break
            try:
                identity = query_identify(target, timeout=min(1.0, deadline-time.monotonic()))
            except (OSError, ValueError):
                identity = None
            if _ready_identity(identity, target):
                if (owned.matches(identity.get('pid')) if owned is not None
                        else identity.get('pid') == child.pid):
                    return 'ready'
            time.sleep(.05)
        raise RecoveryImportError('SERVING_NOT_CONFIRMED')
    except BaseException:
        # This exact Popen child only. Never stop the root host or a PID from an
        # untrusted record. Reap before the caller restores the parked marker.
        if owned is not None:
            owned.stop()
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        raise
    finally:
        if owned is not None:
            owned.close()


def start_imported(service, record, name):
    if not record or record['name'] != name:
        raise RecoveryImportError('IMPORT_REQUIRED')
    from hermes_cli import profiles as host
    verify_host_compatibility(host)
    for module, expected in LIFECYCLE_PINS.items():
        spec=importlib.util.find_spec(module)
        if not spec or hashlib.sha256(Path(spec.origin).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=expected:
            raise RecoveryImportError('HOST_UNAVAILABLE')
    target=core.checked_path(host.get_profile_dir(name))
    if target!=record['target'] or name=='default' or target==service.profile:
        raise RecoveryImportError('INVALID_NAME')
    if record.get('started'):
        # Do not reuse an old ACK as current serving evidence.
        raise RecoveryImportError('START_ALREADY_CONFIRMED')
    if profile_digest(target)!=record['digest']:
        raise RecoveryImportError('IMPORTED_PROFILE_CHANGED')
    from gateway import host_attach,control_socket
    owner=host_attach.host_gateway(wait_for_channel=0)
    # The exact job-owned import above authorizes a new scoped runtime, not
    # adoption of the owner's home. A normal named host is NOT a root mux.
    # Only the independently discovered SDK root-owner contract permits hot
    # serve. Missing/unrelated/standalone owners confer no extra authority;
    # source-host absence deliberately still permits this verified new target.
    root_owner = (owner is not None and not owner.standalone
                  and Path(owner.home) == target.parent.parent)
    marker=target/'gateway.parked'
    with ExitStack() as pinned:
        # Revalidate the imported tree before granting host discovery visibility.
        if profile_digest(target,pinned)!=record['digest']:
            raise RecoveryImportError('IMPORTED_PROFILE_CHANGED')
        marker.unlink()
    try:
        if not root_owner:
            _start_standalone(target)
            answer = None
        else:
            answer = control_socket.request_serve_profile_hot(owner.home,name)
        if root_owner and answer == {'error': 'host multiplexer is not ready'}:
            # The SDK exposes no scoped promotion verb. Its rescan discovers ALL
            # profiles, so restore consent cannot authorize it. Start only this
            # new, inert import through the SDK's ordinary standalone lifecycle.
            _start_standalone(target)
        elif root_owner and (not isinstance(answer,dict) or answer.get('served')!=name
                or answer.get('error') or answer.get('pending')
                or name not in answer.get('served_profiles',[])):
            raise RecoveryImportError('SERVING_NOT_CONFIRMED')
    except Exception:
        # Pending is NOT success. Persist the parked intent again, so a late
        # startup completion is removed by host reconciliation; never stop host.
        with core.regular_output(marker) as fd:
            os.write(fd,b'finix-recovered-review-required\n')
        raise RecoveryImportError('SERVING_NOT_CONFIRMED') from None
    record['started']=True
    return dict(name=name,served=name,integrations_paused=True)
