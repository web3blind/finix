"""Optional, explicitly selected external Google Workspace SKILL transport.

Compatibility contract v1: google-workspace 1.2.0, the pinned source revisions
below, and its build_service('drive', 'v3') function. That function is a named
external-skill Python API, NOT a documented/stable Hermes SDK provider API.
Changing the skill requires compatibility review, not automatic hash adoption.
The skill alone loads/refreshes its grant. Finix never opens credentials.

Integration: explicitly select backend 'workspace-skill'; pass the active owning
profile home and a DIFFERENT private state directory to WorkspaceSkillDrive.
First call WorkspaceSkillRunner(home).identity() in the private connection flow,
show the opaque pin for explicit account selection, persist that pin, then supply
account_digest to the drive. Never substitute this backend for finix:google.
This does not satisfy review-2's finix:google/credentialed_provider_request rule.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import re
import stat

from . import core

CONTRACT = {
    'google_api.py': '7e21f8b277fce049ab833b90b2e3421a40db09e57c3fca39d102905330a30fc8',
    '_hermes_home.py': '190126d124bf673dbb1f4d082013d6f64bd90907ac4ffe89b871ab6c01a1d1b6',
}


class SetupNeeded(core.FinixError):
    def __init__(self, code='SKILL_SETUP_REQUIRED'):
        self.code = code
        super().__init__(code + ': explicitly enable the compatible google-workspace skill in this active profile; '
                         'use its supported setup and review the requested permissions in this profile. No other profile/grant fallback.')


def contract_files(skill):
    paths = [Path(skill)/'SKILL.md', *[Path(skill)/'scripts'/name for name in CONTRACT]]
    text = ''
    for path in paths:
        try:
            with core.regular_input(path) as fd:
                data = core.read_regular(fd, 131072)
        except FileNotFoundError:
            raise SetupNeeded('SKILL_DEPENDENCY_MISSING') from None
        if path.name == 'SKILL.md':
            text = data.decode('utf-8')
        if path.name in CONTRACT and hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest() != CONTRACT[path.name]:
            raise SetupNeeded('SKILL_CONTRACT_CHANGED')
    front = text.split('---', 2)
    if (len(front) != 3 or front[0].strip()
            or not re.search(r'^name:\s*[\"\']?google-workspace[\"\']?\s*$', front[1], re.M)
            or not re.search(r'^version:\s*[\"\']?1\.2\.0[\"\']?\s*$', front[1], re.M)
            or re.search(r'^disabled:\s*true\s*$', front[1], re.M)):
        raise SetupNeeded('SKILL_CONTRACT_CHANGED')
    return paths


def discover_skill(home):
    """Bounded same-profile metadata scan. Never calls skill tools/PM/deps."""
    home = core.checked_path(home)
    root = home/'skills'
    found, pending, count = [], [(root, 0)], 0
    while pending:
        directory, depth = pending.pop()
        if not directory.exists():
            continue
        if directory.is_symlink() or getattr(directory.lstat(), 'st_file_attributes', 0) & 0x400:
            raise SetupNeeded('UNTRUSTED_SKILL')
        count += 1
        if count > 512:
            raise SetupNeeded('SKILL_DISCOVERY_LIMIT')
        marker = directory/'SKILL.md'
        if marker.exists():
            with core.regular_input(marker) as fd:
                text = core.read_regular(fd, 131072).decode('utf-8')
            front = text.split('---', 2)
            if len(front) == 3 and re.search(r'^name:\s*[\"\']?google-workspace[\"\']?\s*$', front[1], re.M):
                found.append(directory)
            continue
        if depth < 4:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.startswith('.'):
                        continue
                    if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0) & 0x400:
                        raise SetupNeeded('UNTRUSTED_SKILL')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((Path(entry.path), depth+1))
    if len(found) != 1:
        raise SetupNeeded('SKILL_MISSING' if not found else 'SKILL_AMBIGUOUS')
    contract_files(found[0])
    return found[0]


def child_environment(home):
    """Keep routing/origin facts without forwarding arbitrary parent secrets."""
    from .workspace_drive import child_env
    env = child_env(home)
    for key in ('HERMES_GATEWAY_SESSION', 'HERMES_SESSION_PLATFORM',
                'HERMES_SINGLE_QUERY_SESSION', 'HERMES_CRON_SESSION', 'HERMES_INTERACTIVE'):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


WORKER = Path(__file__).with_name('workspace_skill_worker.py')
MEMORY_LIMIT = 768 * 1024 * 1024
REPLY_LIMIT = 1024 * 1024


def _native_identity(path, *, directory):
    if directory:
        return WorkspaceDrive._directory_identity(path)
    with core.regular_input(path) as fd:
        if os.name == 'nt':
            import msvcrt
            from . import windows_native as win
            info = win.information(msvcrt.get_osfhandle(fd))
            return [info.volume, info.index_hi, info.index_lo]
        info = os.fstat(fd)
        return [info.st_dev, info.st_ino]


def binding_snapshot(home, skill=None):
    """JSON-native identities, including every lexical ancestor (not realpath)."""
    home = Path(home)
    directories = [*reversed(home.parents), home]
    files = []
    if skill is not None:
        skill = Path(skill)
        skill.relative_to(home)  # Never admit sources from another profile.
        files = contract_files(skill)
        for file in files:
            for directory in reversed(file.parents):
                if directory not in directories:
                    directories.append(directory)
    return [{'path': str(p), 'directory': d, 'identity': _native_identity(p, directory=d)}
            for p, d in [*((p, True) for p in directories), *((p, False) for p in files)]]


class NativeBinding:
    """Retain anchors AND check lexical membership; POSIX fstat alone is not a fence.

    Linux's child-only proc-fd selector keeps Skill credential reads on the
    originally selected root even if its lexical name is replaced later. Checks
    around calls detect cooperative changes, not atomic remote mutation fences.
    """
    def __init__(self, home, records):
        self.home, self.records = Path(home), records
        self.stack = ExitStack()
        self.root_fd = None

    def __enter__(self):
        try:
            if not isinstance(self.records, list) or not self.records or len(self.records) > 128:
                raise SetupNeeded('BINDING_CHANGED')
            for record in self.records:
                path, directory = Path(record['path']), record['directory']
                if os.name == 'nt':
                    import msvcrt
                    from . import windows_native as win
                    if directory:
                        anchor = self.stack.enter_context(win.directory(path))
                        identity = list(anchor.identity())
                    else:
                        fd = self.stack.enter_context(win.regular(path))
                        info = win.information(msvcrt.get_osfhandle(fd))
                        identity = [info.volume, info.index_hi, info.index_lo]
                else:
                    if not __import__('sys').platform.startswith('linux'):
                        raise SetupNeeded('PLATFORM_UNAVAILABLE')
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    fd = os.open(path, flags | (os.O_DIRECTORY if directory else 0))
                    self.stack.callback(os.close, fd)
                    info = os.fstat(fd)
                    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                        raise SetupNeeded('BINDING_CHANGED')
                    identity = [info.st_dev, info.st_ino]
                    if directory and path == self.home:
                        self.root_fd = fd
                if identity != record['identity']:
                    raise SetupNeeded('BINDING_CHANGED')
            if not any(r['directory'] and Path(r['path']) == self.home for r in self.records):
                raise SetupNeeded('BINDING_CHANGED')
            self.check()
            return self
        except BaseException:
            self.stack.close()
            raise

    def check(self):
        try:
            for r in self.records:
                if _native_identity(r['path'], directory=r['directory']) != r['identity']:
                    raise SetupNeeded('BINDING_CHANGED')
        except (OSError, ValueError):
            raise SetupNeeded('BINDING_CHANGED') from None

    def credential_home(self):
        self.check()
        if os.name == 'nt':
            return self.home
        if self.root_fd is None:
            raise SetupNeeded('BINDING_CHANGED')
        selector = Path('/proc/self/fd')/str(self.root_fd)
        # Intentional proc symlink: only this freshly owned fd is admitted.
        info = selector.stat()
        selected = next(r['identity'] for r in self.records if Path(r['path']) == self.home)
        if [info.st_dev, info.st_ino] != selected or selector.resolve(strict=True) != self.home:
            raise SetupNeeded('BINDING_CHANGED')
        return selector

    def __exit__(self, *exc):
        self.stack.close()


class SkillImportClosure:
    """Worker-local resolver: pinned buffers plus the pre-Skill runtime only.

    google_api adds its scripts directory to sys.path. That must not turn *any*
    unreviewed sibling (including dependency packages) into executable input.
    This policy stays installed through dynamic build_service/media imports.
    """
    def __init__(self, home):
        import sys
        self.home = Path(home).resolve(strict=True)
        self.paths = [p for p in sys.path if p and self.outside(p)]
        import types
        for key, module in list(sys.modules.items()):
            origin = getattr(module, '__file__', None)
            spec = getattr(module, '__spec__', None)
            if (key == '_hermes_home'
                    or (isinstance(module, types.ModuleType) and key != '__main__' and not origin and spec is None)
                    or (origin and not self.outside(origin))
                    or any(not self.outside(p) for p in getattr(module, '__path__', []))):
                raise SetupNeeded('SKILL_CONTRACT_CHANGED')

    def outside(self, path):
        try:
            Path(path).resolve().relative_to(self.home)
            return False
        except ValueError:
            return True

    def find_spec(self, fullname, path=None, target=None):
        from importlib.machinery import PathFinder
        paths = self.paths if path is None else [p for p in path if self.outside(p)]
        spec = PathFinder.find_spec(fullname, paths, target)
        if spec is None:
            # Do not fall through to PathFinder using Skill-mutated sys.path.
            raise ModuleNotFoundError(fullname, name=fullname)
        origin = spec.origin
        if ((origin and origin not in {'built-in', 'frozen'} and not self.outside(origin))
                or any(not self.outside(p) for p in (spec.submodule_search_locations or []))):
            raise SetupNeeded('SKILL_CONTRACT_CHANGED')
        return spec


def load_skill(skill):
    """Execute only verified source buffers; never load/rewrite a Skill grant."""
    import sys
    import types
    from importlib.machinery import BuiltinImporter, FrozenImporter
    skill = Path(skill)
    contract_files(skill)
    selector = Path(os.environ['HERMES_HOME'])
    expected_home = selector.resolve(strict=True)
    if skill.resolve(strict=True) != discover_skill(expected_home).resolve(strict=True):
        raise SetupNeeded('PROFILE_CHANGED')
    # Hash the actual bytes compiled, not a prior pathname read. No pyc loader.
    buffers = {}
    for filename, digest in CONTRACT.items():
        with core.regular_input(skill/'scripts'/filename) as fd:
            source = core.read_regular(fd, 131072)
        if hashlib.sha256(source.replace(b'\r\n', b'\n')).hexdigest() != digest:
            raise SetupNeeded('SKILL_CONTRACT_CHANGED')
        buffers[filename] = compile(source, str(skill/'scripts'/filename), 'exec')
    closure = SkillImportClosure(expected_home)
    sys.meta_path[:] = [BuiltinImporter, FrozenImporter, closure]
    # Resolve the genuine public runtime helper before exposing Skill code. If
    # absent, the pinned _hermes_home's documented standalone fallback applies.
    try:
        from hermes_constants import get_hermes_home
    except ModuleNotFoundError as exc:
        if exc.name != 'hermes_constants':
            raise
    else:
        if Path(get_hermes_home()) != selector:
            raise SetupNeeded('PROFILE_CHANGED')
    helper = types.ModuleType('_hermes_home')
    helper.__file__ = str(skill/'scripts'/'_hermes_home.py')
    exec(buffers['_hermes_home.py'], helper.__dict__)
    if Path(helper.get_hermes_home()) != selector:
        raise SetupNeeded('PROFILE_CHANGED')
    sys.modules['_hermes_home'] = helper
    module = types.ModuleType('finix_external_workspace_api')
    module.__file__ = str(skill/'scripts'/'google_api.py')
    exec(buffers['google_api.py'], module.__dict__)
    if Path(module.HERMES_HOME) != selector or Path(module.HERMES_HOME).resolve(strict=True) != expected_home:
        raise SetupNeeded('PROFILE_CHANGED')
    import inspect
    if not callable(getattr(module, 'build_service', None)) or list(inspect.signature(module.build_service).parameters) != ['api', 'version']:
        raise SetupNeeded('SKILL_CONTRACT_CHANGED')
    return module


@contextmanager
def trusted_sources(skill):
    """Hold native no-reparse/no-symlink source and interpreter trust anchors."""
    import sys
    from .workspace_drive import _pin_linux_invocation, trusted_runtime
    binary = Path(sys.executable)
    if not binary.is_absolute():
        raise SetupNeeded('UNTRUSTED_RUNTIME')
    files = [binary.resolve(strict=True), WORKER, Path(__file__), *contract_files(skill)]
    with ExitStack() as stack:
        try:
            prefix, _ = stack.enter_context(trusted_runtime(Path(skill)/'scripts'/'google_api.py', entry=WORKER))
        except core.FinixError:
            raise SetupNeeded('UNTRUSTED_RUNTIME') from None
        if os.name == 'nt':
            import msvcrt
            from . import windows_native as win
            for path in files:
                anchor = stack.enter_context(win.directory(path.parent))
                fd = stack.enter_context(win.regular(path))
                if (not win.trusted_handle(msvcrt.get_osfhandle(fd))
                        or not win.trusted_handle(anchor.handle)
                        or not all(win.trusted_handle(h, ancestor=True) for h in anchor.handles[:-1])):
                    raise SetupNeeded('UNTRUSTED_RUNTIME')
        elif sys.platform == 'linux':
            _pin_linux_invocation(binary, binary.resolve(strict=True), stack)
            for path in files:
                for item in [*reversed(path.parents), path]:
                    fd = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    stack.callback(os.close, fd)
                    info = os.fstat(fd)
                    sticky = stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                    if (info.st_uid not in {0, os.getuid()} or (info.st_mode & 0o022 and not sticky)
                            or not (stat.S_ISDIR(info.st_mode) if item != path else stat.S_ISREG(info.st_mode))):
                        raise SetupNeeded('UNTRUSTED_RUNTIME')
        else:
            raise SetupNeeded('PLATFORM_UNAVAILABLE')
        # Revalidate after native source pins have been acquired, not before.
        contract_files(skill)
        yield prefix


class WorkspaceSkillRunner:
    """One bounded child/service per operation, always preflighted in that child.

    The source process may buffer an individual HTTP response before validation;
    this is bounded by the child OS memory budget, not a network pre-body cap.
    No uploads are retried here. The existing durable journal owns reconciliation.
    """
    def __init__(self, home, *, account_pin=None, enabled=True, timeout=300):
        self.home = core.checked_path(home)
        if enabled is not True:
            raise SetupNeeded('EXPLICIT_SELECTION_REQUIRED')
        if account_pin is not None and not re.fullmatch(r'[a-f0-9]{64}', account_pin):
            raise SetupNeeded('INVALID_ACCOUNT_PIN')
        if not 0 < timeout <= 1800:
            raise ValueError('Invalid skill worker deadline')
        self.account_pin, self.timeout = account_pin, timeout
        self._selection = (str(self.home), account_pin)
        from .workspace_drive import WorkspaceDrive
        self._home_identity = WorkspaceDrive._directory_identity(self.home)
        self._home_binding = binding_snapshot(self.home)
        # Own the selected POSIX object from selection time, not a later reopen.
        # It is not a rename lock. The child independently checks its native ID.
        self._root_fd = None
        if os.name != 'nt':
            import weakref
            self._root_fd = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._root_finalizer = weakref.finalize(self, os.close, self._root_fd)
            info = os.fstat(self._root_fd)
            if [info.st_dev, info.st_ino] != self._home_identity:
                self._root_finalizer()
                raise SetupNeeded('BINDING_CHANGED')
        self.last_reaped, self.last_exit_code = True, None

    def identity(self, *, cancel=None):
        value = self('identity', cancel=cancel)['account_id']
        return {'kind': 'google-drive-permission-id-sha256', 'digest': hashlib.sha256(value.encode()).hexdigest()}

    def __call__(self, operation, *args, expected_size=None, cancel=None):
        import json
        from .streaming import check_cancel
        from .workspace_drive import WorkspaceDrive
        if ((str(self.home), self.account_pin) != self._selection
                or WorkspaceDrive._directory_identity(self.home) != self._home_identity):
            raise SetupNeeded('BINDING_CHANGED')
        check_cancel(cancel)
        skill = discover_skill(self.home)
        records = binding_snapshot(self.home, skill)
        if records[:len(self._home_binding)] != self._home_binding:
            raise SetupNeeded('BINDING_CHANGED')
        request = dict(operation=operation, args=list(args), account_pin=self.account_pin,
                       expected_size=expected_size, skill=str(skill), home=str(self.home), binding=records)
        with trusted_sources(skill) as runtime:
            response = self._child(request, cancel, runtime=runtime)
            NativeBinding(self.home, records).check()
        if not isinstance(response, dict) or set(response) not in ({'result'}, {'error'}):
            raise SetupNeeded('INVALID_RESPONSE')
        if 'error' in response:
            from .workspace_drive import WorkspaceError
            if response['error'] == 'NOT_FOUND':
                raise WorkspaceError('NOT_FOUND')
            raise SetupNeeded(response['error'] if response['error'] in ERROR_CODES else 'PROVIDER_FAILED')
        if not isinstance(response['result'], dict):
            raise SetupNeeded('INVALID_RESPONSE')
        return response['result']

    def _child(self, request, cancel, *, runtime=None):
        import json
        import queue
        import signal
        import subprocess
        import sys
        import threading
        import time
        from .streaming import check_cancel
        env = child_environment(self.home)
        argv = [*(runtime or [sys.executable, '-I', '-B']), str(WORKER)]
        popen = subprocess.Popen
        if os.name == 'nt':
            from . import windows_native as win
            popen = win.popen
        process = popen(argv, env=env, cwd=str(self.home), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.last_reaped = False
        events, stop = queue.Queue(maxsize=8), threading.Event()
        def drain(stream, stdout):
            try:
                while not stop.is_set():
                    chunk = os.read(stream.fileno(), 4096)
                    while not stop.is_set():
                        try:
                            events.put((stdout, chunk), timeout=.1)
                            break
                        except queue.Full:
                            continue
                    if not chunk:
                        break
            except OSError:
                pass
        threads = [threading.Thread(target=drain, args=(stream, flag), daemon=True)
                   for stream, flag in ((process.stdout, True), (process.stderr, False))]
        try:
            if os.name == 'nt':
                # Apply aggregate job memory bound before releasing input to the
                # credential-owning code. The bootstrap only waits for this input.
                import ctypes as C
                from ctypes import wintypes as W
                limits = win.ExtendedLimits()
                limits.basic.flags = 0x2000 | 0x200
                limits.job_memory = MEMORY_LIMIT
                set_job = win.api(win.K, 'SetInformationJobObject', W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
                if not set_job(process._finix_job, 9, C.byref(limits), C.sizeof(limits)):
                    raise SetupNeeded('RESOURCE_LIMIT_UNAVAILABLE')
            for thread in threads:
                thread.start()
            payload = json.dumps(request).encode() + b'\n'
            if len(payload) > 65536:
                raise SetupNeeded('INVALID_REQUEST')
            process.stdin.write(payload)
            process.stdin.close()
            out, count, remaining = bytearray(), 0, 2
            deadline = time.monotonic() + self.timeout
            while remaining:
                check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise SetupNeeded('DEADLINE_EXCEEDED')
                try:
                    stdout, chunk = events.get(timeout=.05)
                except queue.Empty:
                    continue
                if not chunk:
                    remaining -= 1
                    continue
                count += len(chunk)
                if count > REPLY_LIMIT:
                    raise SetupNeeded('RESPONSE_LIMIT')
                if stdout:
                    out.extend(chunk)
            self.last_exit_code = process.wait(timeout=5)
            if self.last_exit_code:
                raise SetupNeeded('WORKER_FAILED')
            try:
                return json.loads(out)
            except (ValueError, UnicodeError, RecursionError):
                raise SetupNeeded('INVALID_RESPONSE') from None
        finally:
            stop.set()
            if os.name == 'nt':
                win.cleanup(process)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            self.last_exit_code = process.returncode
            self.last_reaped = process.poll() is not None
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=5)


from .workspace_drive import WorkspaceDrive


class WorkspaceSkillDrive(WorkspaceDrive):
    """Existing durable ciphertext journal, in an explicitly separate authority."""
    credential_authority = 'google-workspace-skill:drive'

    def __init__(self, home, profile_id, state, *, runner=None, account_digest=None):
        selected = runner if runner is not None else WorkspaceSkillRunner(home, account_pin=account_digest, enabled=True)
        super().__init__(home, profile_id, state, runner=selected, account_digest=account_digest)
        self._skill_runner = selected

    def _check_runner_binding(self):
        if ((str(self.home), self.owner, str(self.state), self.account_digest) != self._selection
                or self.runner is not self._skill_runner or getattr(self.runner, 'home', None) != self.home
                or getattr(self.runner, 'account_pin', None) != self.account_digest
                or self._directory_identity(self.home) != self._home_identity):
            raise SetupNeeded('BINDING_CHANGED')

    def _run(self, operation, *args, **kwargs):
        self._check_runner_binding()
        return super()._run(operation, *args, **kwargs)

    def _binding(self):
        self._check_runner_binding()
        value = super()._binding()
        value['provider'] = 'workspace-skill'
        return value

    def _read_private(self, name):
        value = super()._read_private(name)
        if value is not None and (not isinstance(value, dict) or value.get('credential_authority') != self.credential_authority):
            raise SetupNeeded('JOURNAL_AUTHORITY_MISMATCH')
        return value

    def _write_private(self, name, value):
        value['credential_authority'] = self.credential_authority
        if 'record' in value:
            value['record']['credential_authority'] = self.credential_authority
        for row in value.get('records', []):
            if row.get('credential_authority') != self.credential_authority:
                raise SetupNeeded('JOURNAL_AUTHORITY_MISMATCH')
        super()._write_private(name, value)

    def descriptor(self, row):
        if row.get('credential_authority') != self.credential_authority:
            raise SetupNeeded('JOURNAL_AUTHORITY_MISMATCH')
        return super().descriptor(row)

    def status(self):
        try:
            discover_skill(self.home)
            return {'ready': False, 'code': 'SKILL_CONNECTION_CHECK_REQUIRED', 'live_google_verified': False,
                    'action': 'Explicitly check the Workspace skill connection and select its account in this profile.'}
        except SetupNeeded as exc:
            return {'ready': False, 'code': exc.code, 'live_google_verified': False, 'action': str(exc)}


ERROR_CODES = frozenset({'NOT_FOUND', 'ACCOUNT_CHANGED', 'INVALID_IDENTITY', 'INVALID_RESPONSE',
    'INVALID_REQUEST', 'UNCERTAIN_ACK', 'RESPONSE_LIMIT', 'PROVIDER_FAILED', 'SKILL_SETUP_REQUIRED',
    'SKILL_CONTRACT_CHANGED', 'PROFILE_CHANGED', 'BINDING_CHANGED', 'UNSUPPORTED_OPERATION'})
