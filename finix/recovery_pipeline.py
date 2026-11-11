"""Ordinary NEW-copy completion shared by registered HTTP and CLI.

Capabilities are server-owned object identities, scoped to one verified handoff.
This slice deliberately preserves ambiguous partial targets as interrupted; it
never retries a nonempty history store or claims durable full resume.
"""
import asyncio
from contextlib import ExitStack, closing, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time

from . import core
from .recovery_import import RecoveryImporter, RecoveryImportError, verify_host_compatibility
from .recovery_activation import LIFECYCLE_PINS, profile_digest, _ready_identity, _WindowsLaunch
from .recovery_shared_host import SharedTargetRuntime, shared_host as _shared_host

_CAPS = {}
_OWNED_TARGETS = {}
RUNTIME_PINS = {
    'gateway.run_bootstrap': 'a84874f85de715a4b55ae5826e62275bf2b6d9a8b8a1f0373b600902502964bb',
    'gateway.runtime_ownership': '5c563b48fd9897bcf348fab1bcf74a7a094d88118ca8071b07189ffeeb5fbf51',
    'gateway.host_rendezvous': '52d4e5a5ee3d1802f0a4d0fd06a482ff46c783b5befe3d85a2ecd155fe7cc903',
    'hermes_cli.gateway_runtime': '0fa5838763c1f0f3798591d9b42acf1127f0595c1a38efb6d9027902cb791c59',
    'hermes_constants': '74d849629c176703d48f9aa43a245fd8819545ac908c805e347db7d691976510',
    'hermes_state': 'dd50e9091ce6b9ebd76d157661bf82aa135d9c6bfe56ec57aed02b3cf901c4f2',
    'hermes_state_mutation_binding': '8cb4e89846f5bbfce5e925df3869663e456a122e858cdc168f6b6d694a034e46',
    'gateway.session_mutations': '457b503c9a4615de4c60ff81eb1db4eab1fae1b124ae2a1cac63f3d48f548caa',
    'gateway.owner_identity': '4cb7810a49a5c3f2ddec645d00ecc90ac7b86edb862e16ea8cf4b039408546be',
    'hermes_cli.gateway_client': '4edfde2b7c5f62cf8bdf6aabc9fc39371153a2ea5f93e6910025f41090f32a21',
    'gateway.runtime_bootstrap_windows': '7f39768e31e6082714304b32d75c175e3534d23d07aa1b1b31ac9e645668e32f',
    'gateway.session_contract': '624f45de8dcf2537266614ac14eaf98150ad6ed82ac3167f30f6f463733e7a41',
}


def _fail(code):
    raise RecoveryImportError(code)


def _identity(path):
    # Admission is a retained native no-follow walk, NOT a second flock on the
    # same directory already held by profile_digest's transaction stack.
    if os.name == 'nt':
        from .windows_native import canonical_directory
        return canonical_directory(path)[1]
    from .recovery import _PosixAnchor
    with ExitStack() as retained:
        anchor = _PosixAnchor(retained, path, owned_from=path, private_from=path)
        anchor.check()
        st = os.fstat(anchor.fd)
        return st.st_dev, st.st_ino


def _pins():
    for name, expected in (LIFECYCLE_PINS | RUNTIME_PINS).items():
        spec = importlib.util.find_spec(name)
        if not spec or hashlib.sha256(Path(spec.origin).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != expected:
            _fail('HOST_UNAVAILABLE')
    from hermes_cli import profiles
    verify_host_compatibility(profiles)


def _peer(target, expected_pid):
    """Authenticate kernel peer before relying on an exact SDK identify reply."""
    if os.name == 'nt':
        import _winapi
        from ctypes import wintypes
        import ctypes
        from gateway.control_socket import windows_pipe_name
        from gateway.runtime_bootstrap_windows import _peer_subject, _PIPE_CLIENT_SQOS
        handle = _winapi.CreateFile(windows_pipe_name(target), 0xC0000000, 0,
            _winapi.NULL, _winapi.OPEN_EXISTING,
            _winapi.FILE_FLAG_OVERLAPPED | _PIPE_CLIENT_SQOS, _winapi.NULL)
        try:
            _peer_subject(handle, server=True)
            pid = wintypes.ULONG()
            get = ctypes.WinDLL('kernel32', use_last_error=True).GetNamedPipeServerProcessId
            get.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
            get.restype = wintypes.BOOL
            if not get(handle, ctypes.byref(pid)) or pid.value != expected_pid:
                _fail('SERVING_NOT_CONFIRMED')
        finally:
            _winapi.CloseHandle(handle)
    else:
        import socket
        import struct
        from hermes_cli.gateway_runtime_discovery import connect_private
        with connect_private(target, 2) as peer:
            pid, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != expected_pid or uid != os.getuid():
                _fail('SERVING_NOT_CONFIRMED')


class _TargetRuntime:
    def __init__(self, target):
        from hermes_cli.gateway_runtime_start import spawn_unmanaged_gateway
        self.target = target
        self.child = spawn_unmanaged_gateway(target, deadline=time.monotonic() + 60)
        self.native = _WindowsLaunch(self.child) if os.name == 'nt' else None

    def ready(self, timeout=60):
        from hermes_cli.gateway_runtime_discovery import query_identify
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.child.poll() is None:
            if self.native:
                self.native.capture()
            try:
                answer = query_identify(self.target, timeout=.5)
            except (OSError, ValueError):
                answer = None
            if _ready_identity(answer, self.target):
                pid = answer.get('pid')
                if self.native.matches(pid) if self.native else pid == self.child.pid:
                    _peer(self.target, pid)
                    return answer
            time.sleep(.05)
        _fail('SERVING_NOT_CONFIRMED')

    def cleanup(self):
        if self.native:
            self.native.stop()
        if self.child.poll() is None:
            self.child.terminate()
            self.child.wait(timeout=15)
        if self.native:
            self.native.close()


def _mint(service, job, profile_id, activation, name, imported):
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_default_hermes_root
    _pins()
    handoff = service.handoff(job, profile_id, activation)
    target = core.checked_path(get_profile_dir(name))
    root = core.checked_path(get_default_hermes_root())
    if (name == 'default' or target == service.profile or target.parent != root / 'profiles'
            or imported.get('name') != name or imported.get('parked') is not True):
        _fail('HISTORY_TARGET_MISMATCH')
    if os.name == 'nt':
        from .windows_native import canonical_directory
        target, _ = canonical_directory(target)
        root, _ = canonical_directory(root)
    with ExitStack() as retained:
        digest = profile_digest(target, retained)
        if 'gateway.parked' not in digest:
            _fail('IMPORT_REQUIRED')
        original = handoff.get('new_copy_payload')
        if imported.get('history_pending'):
            preserved = target / '.finix-paused/history-original/state.db'
            if original != {'size': preserved.stat().st_size, 'sha256': core.digest_file(preserved)}:
                _fail('HISTORY_SOURCE_BINDING_REQUIRED')
        native_stack = ExitStack()
        anchors = []
        try:
            if os.name == 'nt':
                from .windows_native import directory, trusted_handle
                for path in (root, target):
                    anchor = native_stack.enter_context(directory(path))
                    if (not trusted_handle(anchor.handle)
                            or any(not trusted_handle(h, ancestor=True) for h in anchor.handles[:-1])):
                        _fail('HISTORY_TARGET_MISMATCH')
            else:
                from .recovery import _PosixAnchor
                pool = {}
                for path in (root, target):
                    anchors.append(_PosixAnchor(native_stack, path, owned_from=root,
                        private_from=root, retained=pool))
        except BaseException:
            native_stack.close()
            raise
        cap = object()
        _CAPS[cap] = dict(service=service, job=job, profile_id=profile_id,
            activation=activation, name=name, target=target, root=root,
            root_identity=_identity(root), native=_identity(target), digest=digest,
            handoff=handoff, original=original, native_stack=native_stack, anchors=anchors,
            purpose='ordinary-new-copy-bootstrap')
        return cap


def _admit(cap):
    record = _CAPS.get(cap)
    if record is None or record['purpose'] != 'ordinary-new-copy-bootstrap':
        _fail('IMPORT_REQUIRED')
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_default_hermes_root
    _pins()
    service = record['service']
    for anchor in record['anchors']:
        anchor.check()
    service._fence(record['profile_id'], record['activation'])
    handoff = (record['cleaned_handoff'] if 'cleaned_handoff' in record else
        service.handoff(record['job'], record['profile_id'], record['activation']))
    if (handoff != record['handoff']
            or record['original'] != record['handoff'].get('new_copy_payload')
            or _identity(record['target']) != record['native']
            or _identity(record['root']) != record['root_identity']
            or not Path(get_profile_dir(record['name'])).samefile(record['target'])
            or not Path(get_default_hermes_root()).samefile(record['root'])):
        _fail('HISTORY_TARGET_MISMATCH')
    return record


def _bootstrap(cap):
    record = _admit(cap)
    target = record['target']
    shared = _shared_host(record)

    # This is the LAST inert whole-tree check. SDK startup legitimately mutates it.
    with ExitStack() as retained:
        if profile_digest(target, retained) != record['digest']:
            _fail('IMPORTED_PROFILE_CHANGED')
        from .recovery_import import _read, _yaml_mapping, MAX_CONFIG
        config = _yaml_mapping(_read(target / 'config.yaml', MAX_CONFIG))
        if shared is not None:
            shared.check()
        _admit(cap)
        config.setdefault('gateway', {}).update(standalone=shared is None, multiplex_profiles=False)
        core.atomic_json(target / 'config.yaml', config)
        _admit(cap)
        if shared is not None:
            shared.check()
        (target / 'gateway.parked').unlink()
    _admit(cap)
    if shared is not None:
        shared.check()
        shared.ensure()
        owned = shared
    else:
        owned = _TargetRuntime(target)
        _OWNED_TARGETS[(id(record['service']), record['job'])] = owned
    identity = owned.ready()
    record.update(runtime=owned, identity=identity)
    return record


async def _readback(record):
    from hermes_cli.gateway_client import connect_gateway, _session_ticket
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    import urllib.request
    target = record['target']
    plan = json.loads((target / '.finix-paused/history-original/plan.json').read_text(encoding='utf8'))
    endpoint = discover_gateway_endpoint(target).endpoint
    if isinstance(record['runtime'], SharedTargetRuntime):
        record['runtime'].endpoint(endpoint)
    token = set_hermes_home_override(target)
    try:
        async with connect_gateway(endpoint) as rpc:
            listed = await rpc.rpc('session.list', scope='stored')
        sessions = plan['sessions']
        by_id = {s['id']: s for s in sessions}
        def root(sid):
            while by_id[sid].get('parent_session_id'):
                sid = by_id[sid]['parent_session_id']
            return sid
        roots = {root(s['id']) for s in sessions}
        if len(listed['sessions']) != len(roots):
            _fail('HISTORY_TRUNCATED')
        total = 0
        for row in listed['sessions']:
            sid = row['session_id']
            if sid not in by_id:
                _fail('HISTORY_ID_COLLISION')
            expected = [m['content'] for s in sessions if root(s['id']) == root(sid)
                for m in s['messages'] if m.get('active', True)]
            ticket = await asyncio.to_thread(_session_ticket, target, endpoint, purpose='native-http')
            request = urllib.request.Request(endpoint.api_origin + '/api/sessions/' + sid + '/messages',
                headers={'X-Hermes-Gateway-Ticket': ticket})
            def read():
                with urllib.request.urlopen(request, timeout=15) as response:
                    return json.load(response)
            answer = await asyncio.to_thread(read)
            if [m['content'] for m in answer['messages']] != expected:
                _fail('HISTORY_RICH_DATA_LOSS')
            total += len(expected)
        with closing(sqlite3.connect((target / 'state.db').as_uri() + '?mode=ro', uri=True)) as db:
            physical = db.execute('select count(*) from messages').fetchone()[0]
            if physical != sum(len(s['messages']) for s in sessions):
                _fail('HISTORY_TRUNCATED')
        if isinstance(record['runtime'], SharedTargetRuntime):
            record['runtime'].ready()
        return dict(stored_logical=len(roots), displayed_messages=total, physical_messages=physical)
    finally:
        reset_hermes_home_override(token)


def _owner_paths(service):
    root = core.private_dir(service.profile / 'cache/finix/recovery-pipeline')
    return root, root / 'current.json', root / 'worker.lock'


def _owner_read(path, profile_id, activation):
    try:
        with core.regular_input(path) as fd:
            state = json.loads(core.read_regular(fd, 256 * 1024))
    except FileNotFoundError:
        return None
    if state.get('profile_id') != profile_id or state.get('activation') != activation:
        _fail('RECOVERY_INTERRUPTED')
    return state


def pipeline_current(service, *, profile_id, activation):
    """Read independently of SDK/history work; never recreate capabilities."""
    from .backup_jobs import state_lease, _busy
    service._fence(profile_id, activation)
    root, path, worker = _owner_paths(service)
    with state_lease(root / 'state.lock'):
        state = _owner_read(path, profile_id, activation)
        if state and state['state'] == 'running' and not _busy(worker):
            state.update(state='interrupted', error='RECOVERY_INTERRUPTED')
            core.atomic_json(path, state)
        service._fence(profile_id, activation)
        return {k: v for k, v in state.items() if k != 'activation'} if state else None


@contextmanager
def pipeline_owner(service, job, *, profile_id, activation, name):
    from .backup_jobs import file_lease, state_lease
    import re
    if not isinstance(job, str) or not re.fullmatch('[a-f0-9]{32}', job):
        _fail('RECOVERY_INTERRUPTED')
    service._fence(profile_id, activation)
    root, path, worker = _owner_paths(service)
    with ExitStack() as retained:
        with state_lease(root / 'state.lock'):
            try:
                retained.enter_context(file_lease(worker))
            except core.FinixError as exc:
                if str(exc) == 'JOB_BUSY':
                    _fail('BUSY')
                raise
            prior = _owner_read(path, profile_id, activation)
            if prior and (prior['state'] != 'succeeded' or prior['id'] == job):
                # A durable receipt is historical proof, not a recreated runtime
                # capability. Do not clobber it when an old request is replayed.
                _fail('RECOVERY_INTERRUPTED')
            state = dict(id=job, profile_id=profile_id, activation=activation,
                name=name, state='running', phase='importing', cancellable=False,
                full_acceptance=False)
            core.atomic_json(path, state)
        def publish(phase, receipt=None):
            service._fence(profile_id, activation)
            with state_lease(root / 'state.lock'):
                state['phase'] = phase
                if receipt is not None:
                    state.update(state='succeeded', receipt=receipt)
                core.atomic_json(path, state)
        try:
            yield publish
        finally:
            if state['state'] == 'running':
                # Cleanup/retained-runtime handling is performed by the inner
                # pipeline before this owner is released, including exceptions.
                with state_lease(root / 'state.lock'):
                    state.update(state='interrupted', error='RECOVERY_INTERRUPTED')
                    core.atomic_json(path, state)


def complete_restore(service, job, *, profile_id, activation, name, acknowledge_incomplete=False, phase=None):
    with pipeline_owner(service, job, profile_id=profile_id, activation=activation, name=name) as publish:
        def progress(value):
            publish(value)
            if phase:
                phase(value)
        receipt = _complete_restore(service, job, profile_id=profile_id,
            activation=activation, name=name, acknowledge_incomplete=acknowledge_incomplete, phase=progress)
        publish('backend_target_ready', receipt)
        return receipt


def _complete_restore(service, job, *, profile_id, activation, name, acknowledge_incomplete=False, phase=None):
    """One owner from verified decrypt through cleanup and fresh target READY.

    Input names are generated by registered adapters, never bootstrap authority.
    A prior/partial operation is preserved, not silently retried or overwritten.
    """
    path = core.private_dir(service.profile / 'cache/finix/recovery-pipeline') / (job + '.json')
    if path.exists():
        _fail('RECOVERY_INTERRUPTED')
    state = dict(job=job, name=name, state='running', phase='importing', full_acceptance=False)
    def update(value):
        service._fence(profile_id, activation)
        state['phase'] = value
        core.atomic_json(path, state)
        if phase:
            phase(value)
    cap = None
    try:
        update('importing')
        imported = RecoveryImporter(service).import_job(job, profile_id=profile_id,
            activation=activation, name=name, consent_import=True,
            acknowledge_incomplete=acknowledge_incomplete, history_mode='new-copy')
        cap = _mint(service, job, profile_id, activation, name, imported)
        update('target_bootstrap')
        record = _bootstrap(cap)
        if isinstance(record['runtime'], SharedTargetRuntime):
            # Private actual SDK evidence, never a client-supplied routing hint.
            state['shared_host'] = dict(identity=record['identity'], serve_ack=record['serve_ack'])
            core.atomic_json(path, state)
        history = None
        if imported.get('history_pending'):
            update('history_importing')
            from .portable_history import complete_target
            asyncio.run(complete_target(record['target'], name=name, expected_original=record['original'],
                shared_runtime=record['runtime'] if isinstance(record['runtime'], SharedTargetRuntime) else None))
            _admit(cap)
            update('verifying_target')
            history = asyncio.run(_readback(record))
            if core.digest_file(record['target'] / '.finix-paused/history-original/state.db') != record['original']['sha256']:
                _fail('HISTORY_SOURCE_BINDING_REQUIRED')
            if core.excluded('.finix-paused/history-original/state.db'):
                _fail('HISTORY_SOURCE_BINDING_REQUIRED')
        _admit(cap)
        identity = record['runtime'].ready(timeout=3)
        if any(identity.get(k) != record['identity'].get(k) for k in ('pid','instance_id','authority_epoch','hermes_home')):
            _fail('SERVING_NOT_CONFIRMED')
        update('cleanup')
        retired = service.cleanup_managed_result(job)
        if retired != record['handoff']:
            _fail('HISTORY_TARGET_MISMATCH')
        record['cleaned_handoff'] = retired
        # Cleanup retires the service handoff. Re-admit its exact retirement
        # receipt plus live source/native/SDK authority, then query READY anew.
        _admit(cap)
        final_identity = record['runtime'].ready(timeout=3)
        if any(final_identity.get(k) != record['identity'].get(k) for k in ('pid','instance_id','authority_epoch','hermes_home')):
            _fail('SERVING_NOT_CONFIRMED')
        service._fence(profile_id, activation)
        receipt = dict(name=name, served=name, integrations_paused=True, can_activate=True,
            archive_verified=True, content_verified=True, backend_target_ready=True,
            full_acceptance=False, foreground_connected=False, history_pending=False,
            complete=imported['complete'], omitted_count=imported['omitted_count'],
            history=history, contract='sdk-new-content-copy-v1')
        if isinstance(record['runtime'], SharedTargetRuntime):
            receipt['bootstrap_contract'] = dict(version='cooperative-root-boundaries-v1',
                condition='no-concurrent-same-user-target-replacement',
                atomic_target_binding=False, checked_after_cleanup=True)
        state.update(state='succeeded', phase='backend_target_ready', receipt=receipt)
        core.atomic_json(path, state)
        return receipt
    except BaseException:
        state.update(state='interrupted', error='RECOVERY_INTERRUPTED', target_preserved=True)
        core.atomic_json(path, state)
        # Preserve partial imported content; terminate ONLY retained owned fixture/runtime.
        owned = _OWNED_TARGETS.pop((id(service), job), None)
        if owned:
            owned.cleanup()
        raise
    finally:
        if cap is not None:
            retained = _CAPS.pop(cap, None)
            if retained is not None:
                retained['native_stack'].close()
