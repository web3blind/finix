"""Profile-owned recovery orchestration; INTERNAL trusted Python API, not a REST API.

The host supplies protected roots (all profiles, storage, state and original
extras), activation tokens and trusted source readers. Never deserialize those
arguments from a renderer. Identity is a user-selected local file or binary
stream; no key is retained by the service. Public dictionaries contain only
closed codes, opaque IDs, numeric progress and safe aggregate completion data.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import copy
from contextvars import copy_context
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import uuid

from . import core, cloud_files


class RecoveryError(core.FinixError):
    """Closed public diagnostic; no exception/provider text crosses this boundary."""
    CODES = frozenset({'STALE_PROFILE', 'INVALID_CANDIDATE', 'INVALID_SOURCE',
        'INVALID_DESTINATION', 'INVALID_IDENTITY', 'BUSY', 'CLOSED', 'NOT_FOUND',
        'INTEGRITY_FAILED', 'RESTORE_FAILED', 'CANCELLED'})

    def __init__(self, code):
        self.code = code if code in self.CODES else 'RESTORE_FAILED'
        super().__init__(self.code)


class _CleanupFailure(RecoveryError):
    def __init__(self):
        super().__init__('RESTORE_FAILED')


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _completion(row):
    completion = cloud_files.known_completion(row)
    if completion is None:
        try:
            completion = cloud_files.completion_record(row,row)
        except (core.FinixError,KeyError,TypeError):
            pass
    return completion or {}


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _identity(info):
    return info.st_dev, info.st_ino


def _trusted_directory(fd, private=False, owned=False):
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}
            or (private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700))
            or (owned and (info.st_uid != os.geteuid() or info.st_mode & 0o022))
            or (not private and info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
        raise RecoveryError('RESTORE_FAILED')


class _PosixAnchor:
    """Walk from / using openat(O_NOFOLLOW); retain every ancestor identity."""
    def __init__(self, stack, path, *, private_from=None, owned_from=None, create=False, retained=None):
        self.path = Path(path)
        self.fds, self.links = [], []
        # Optional transaction-owned prefix pool. Entries never relinquish their
        # native identity before the owning stack closes; reuse is NOT a reopen.
        pool = retained if retained is not None else {}
        fd = pool.get(Path('/'))
        if fd is None:
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            pool[Path('/')] = fd
        self.fds.append(fd)
        current = Path('/')
        _trusted_directory(fd)
        for name in self.path.parts[1:]:
            current = current / name
            private = private_from is not None and (current == private_from or private_from in current.parents)
            owned = owned_from is not None and (current == owned_from or owned_from in current.parents)
            if create and (private or owned):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = pool.get(current)
            if child is None:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, child)
                pool[current] = child
            # A replaced prefix must not be silently adopted from the pathname.
            if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(os.fstat(child)):
                raise RecoveryError('RESTORE_FAILED')
            _trusted_directory(child, private, owned)
            self.links.append((fd, name, child))
            self.fds.append(child)
            fd = child
        self.fd = fd
        self.check()

    def check(self):
        for parent, name, child in self.links:
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(os.fstat(child)):
                raise RecoveryError('RESTORE_FAILED')

    @property
    def path_fd(self):
        # Only private/internal code uses this capability path. Never checked_path:
        # /proc's deliberate fd symlink is not an untrusted filesystem traversal.
        return Path('/proc/self/fd') / str(self.fd)


def _erase_directory(fd):
    """No-follow descriptor-relative cleanup, including renamed ancestors."""
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if _identity(info) != _identity(os.fstat(child)):
                    raise RecoveryError('RESTORE_FAILED')
                _erase_directory(child)
                if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(info):
                    raise RecoveryError('RESTORE_FAILED')
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=fd)


@contextmanager
def _stage_area(service):
    if os.name == 'nt':
        with ExitStack() as stack:
            # Native guards hold all parents without delete sharing. Validate ACLs
            # on every private component, not just the final restore directory.
            for path in [service.profile, service.profile / 'cache',
                         service.profile / 'cache/finix', service.stage_root]:
                core.private_dir(path)
                stack.enter_context(core.locked(path))
            core.validate_sqlite_temp_location(service.stage_root, service.profile)
            stage = core.private_dir(service.stage_root / uuid.uuid4().hex)
            try:
                with core.locked(stage):
                    yield stage, lambda: None
            finally:
                try:
                    shutil.rmtree(stage)
                except OSError:
                    raise _CleanupFailure() from None
        return
    with ExitStack() as stack:
        if not core.excluded(service.stage_root.relative_to(service.profile).as_posix()):
            raise RecoveryError('RESTORE_FAILED')
        # Trust ordinary owner-controlled profile/cache ancestors; only the
        # secret staging subtree must be exactly private. Never chmod ancestors.
        root = _PosixAnchor(stack, service.stage_root, owned_from=service.profile,
                            private_from=service.stage_root, create=True)
        for directory in root.fds[-4:]:
            stack.enter_context(core.locked('/proc/self/fd/' + str(directory) + '/.'))
        name = uuid.uuid4().hex
        os.mkdir(name, mode=0o700, dir_fd=root.fd)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root.fd)
        stack.callback(os.close, fd)
        def check():
            root.check()
            for directory in root.fds[-4:-1]:
                _trusted_directory(directory, owned=True)
            _trusted_directory(root.fd, True)
            _trusted_directory(fd, True)
            if _identity(os.stat(name, dir_fd=root.fd, follow_symlinks=False)) != _identity(os.fstat(fd)):
                raise RecoveryError('RESTORE_FAILED')
        try:
            check()
            yield Path('/proc/self/fd') / str(fd), check
        finally:
            # The handle outlives cleanup; never retry via the old pathname.
            try:
                _erase_directory(fd)
                if _identity(os.stat(name, dir_fd=root.fd, follow_symlinks=False)) != _identity(os.fstat(fd)):
                    raise _CleanupFailure()
                os.rmdir(name, dir_fd=root.fd)
            except (OSError, RecoveryError):
                raise _CleanupFailure() from None


def _restore_anchored(cipher, key, target, *, cancel, progress):
    """Linux counterpart of core.restore, retaining its exact stream admission.

    No checked_path call can safely consume an fd capability: it intentionally
    rejects /proc fd links. Open the two stage-only files with no-follow and pass
    the private key descriptor only to age; stdout goes directly to the existing
    authenticated, bounded manifest/scope parser, never to a plaintext tempfile.
    """
    if os.name == 'nt':
        return core.restore(cipher, key, target, cancel=cancel, progress=progress)
    import subprocess
    from . import streaming
    process = None
    with ExitStack() as stack:
        cipherfd = os.open(cipher, os.O_RDONLY | os.O_NOFOLLOW)
        stack.callback(os.close, cipherfd)
        keyfd = os.open(key, os.O_RDONLY | os.O_NOFOLLOW)
        stack.callback(os.close, keyfd)
        for fd in (cipherfd, keyfd):
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RecoveryError('RESTORE_FAILED')
        extract = target.parent / 'extract'
        extract.mkdir(mode=0o700)
        try:
            process = subprocess.Popen(['age', '-d', '-i', f'/proc/self/fd/{keyfd}'],
                stdin=cipherfd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(keyfd,))
            payload, count, layout, completion = streaming.restore_stream(
                streaming.Pipe(process.stdout, cancel), extract, cancel, progress)
            streaming._wait(process, cancel)
            core.publish_directory(payload, target)
            return dict(files=count, layout=layout, cron_disabled=True, **completion)
        finally:
            if process is not None:
                streaming._cleanup(process)


def _pin_protected(stack, paths):
    pins = []
    for path in paths:
        parent = _PosixAnchor(stack, path.parent)
        try:
            fd = os.open(path.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent.fd)
        except FileNotFoundError:
            pins.append((parent, None))
            continue
        stack.callback(os.close, fd)
        if stat.S_ISLNK(os.fstat(fd).st_mode):
            raise RecoveryError('INVALID_DESTINATION')
        parent.check()
        pins.append((parent, fd))
    return pins


@contextmanager
def _publish_parent(destination, protected, *, expected_parent=None, protected_pins=None):
    if os.name == 'nt':
        with core.locked(destination.parent):
            yield destination
        return
    with ExitStack() as stack:
        parent = _PosixAnchor(stack, destination.parent)
        if expected_parent is not None:
            expected_parent.check()
            if _identity(os.fstat(parent.fd)) != _identity(os.fstat(expected_parent.fd)):
                raise RecoveryError('INVALID_DESTINATION')
        # Compare protected physical identities AFTER pinning the destination.
        # These protected handles were acquired before final path validation.
        ancestry = {_identity(os.fstat(fd)) for fd in parent.fds}
        target = parent.path_fd / destination.name
        target_id = _identity(target.stat(follow_symlinks=False)) if target.exists() else None
        pins = protected_pins if protected_pins is not None else _pin_protected(stack, protected)
        for anchor, fd in pins:
            if ((fd is not None and (_identity(os.fstat(fd)) in ancestry
                                      or target_id == _identity(os.fstat(fd))))
                    or (target_id is not None and target_id in {_identity(os.fstat(f)) for f in anchor.fds})):
                raise RecoveryError('INVALID_DESTINATION')
            anchor.check()
        parent.check()
        # Retain the original operation's nonblocking directory serialization.
        with core.locked(str(parent.path_fd) + '/.'):
            yield target
        parent.check()


@dataclass(frozen=True)
class CommittedSource:
    """Trusted provider capability, constructed by the host, never the client.

    records() returns ONLY exact committed journal descriptors after validating
    ownership/profile/connection binding. transfer(record, writable, cancel)
    downloads that exact descriptor, not a supplied remote ID. Both must honor
    bounded provider deadlines; transfer must check cancel between chunks.
    Connection generation must change when account/root/config binding changes.
    Service re-reads the journal before AND after transfer. No inferred remote
    listing, pending intents, or arbitrary remote IDs are admitted.
    """
    connection: str
    generation: str
    records: object
    transfer: object


def committed_journal_source(*, connection, generation, journal, expected_binding,
                             validate_record, download):
    """Build a capability for rclone/workspace/Drive/managed committed journals.

    Host supplies the *exact* provider binding (profile/account/root/namespace),
    its descriptor validator, and a bounded exact-descriptor download wrapper.
    This deliberately never calls ledger() on an absent journal: some provider
    ledger methods CREATE cloud folders. Drive search/listing is not a journal.
    No upload, OAuth, remote listing or retention is initiated here.
    """
    journal = core.checked_path(journal)
    binding = copy.deepcopy(expected_binding)
    if not binding:
        raise RecoveryError('INVALID_SOURCE')
    def records():
        if not journal.exists():
            return []
        with core.regular_input(journal) as fd:
            saved = json.loads(core.read_regular(fd, 1024 * 1024))
        if (not isinstance(saved, dict) or any(saved.get(k) != v for k, v in binding.items())
                or not isinstance(saved.get('records'), list)):
            raise RecoveryError('INVALID_SOURCE')
        rows, seen = [], set()
        for row in saved['records']:
            if not isinstance(row, dict) or validate_record(copy.deepcopy(row)) is False:
                raise RecoveryError('INVALID_SOURCE')
            encoded = _encoded(row)
            if encoded in seen:
                raise RecoveryError('INVALID_SOURCE')
            seen.add(encoded)
            rows.append(copy.deepcopy(row))
        return rows
    def transfer(row, output, cancel):
        if row not in records():
            raise RecoveryError('INVALID_CANDIDATE')
        if cancel():
            raise RecoveryError('CANCELLED')
        download(copy.deepcopy(row), output, cancel)
        if cancel():
            raise RecoveryError('CANCELLED')
        if row not in records():
            raise RecoveryError('INVALID_CANDIDATE')
    return CommittedSource(connection, generation, records, transfer)


@dataclass(frozen=True, eq=False)
class _ManagedDestination:
    """Unserializable server authority; identity membership, not caller fields."""
    path: Path
    identities: tuple


class RecoveryService:
    """One worker, no queue; bounded retained jobs and catalog. Call close()."""
    def __init__(self, *, profile_id, profile_root, activation, protected_roots,
                 max_jobs=32, max_candidates=4096, authority_guard=None):
        if not profile_id or not activation or not 1 <= max_jobs <= 256 or not 1 <= max_candidates <= 65536:
            raise RecoveryError('INVALID_SOURCE')
        self.profile_id, self.activation = profile_id, activation
        self.profile = core.checked_path(profile_root)
        self.protected = [self.profile, *map(core.checked_path, protected_roots)]
        self.stage_root = self.profile / 'cache/finix/restore'
        self._sources, self._catalog, self._jobs = {}, {}, {}
        self._handoffs = {}
        self._managed = set()
        self.recovered_root = self.profile / 'cache/finix/recovered'
        self._lock = threading.RLock()
        self._worker = None
        self._closed = False
        self._epoch = 0
        self._max_jobs, self._max_candidates = max_jobs, max_candidates
        self._authority_guard = authority_guard

    def _check_authority(self):
        if self._authority_guard is not None:
            self._authority_guard.check()

    def _fence(self, profile_id, activation):
        self._check_authority()
        if self._closed:
            raise RecoveryError('CLOSED')
        if self._epoch or profile_id != self.profile_id or activation != self.activation:
            raise RecoveryError('STALE_PROFILE')

    def add_local(self, connection, output, state):
        """Register fixed trusted paths; local filenames still come from index admission."""
        output, state = core.checked_path(output), core.checked_path(state)
        def records():
            index = core.load_state(state)
            if not index:
                return []
            if (index.get('output') != str(output) or index.get('source') !=
                    hashlib.sha256(str(self.profile).encode()).hexdigest()):
                raise RecoveryError('INVALID_SOURCE')
            rows = []
            for row in index['backups']:
                path = core.checked_path(output / row['name'])
                with core.regular_input(path) as fd:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise RecoveryError('INVALID_SOURCE')
                rows.append(dict(row, size=info.st_size))
            return rows
        def transfer(row, stream, cancel):
            path = core.checked_path(output / row['name'])
            with core.regular_input(path) as fd:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RecoveryError('INVALID_SOURCE')
                while True:
                    if cancel():
                        raise RecoveryError('CANCELLED')
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
        with self._lock:
            self.protected.extend([output, state])
            self.add_source(CommittedSource(connection, 'local-index-v1', records, transfer))

    def add_source(self, source):
        """Register a trusted committed-journal provider capability."""
        with self._lock:
            if self._closed:
                raise RecoveryError('CLOSED')
            if self._worker and self._worker.is_alive():
                raise RecoveryError('BUSY')
            if not isinstance(source, CommittedSource) or not source.connection or not source.generation:
                raise RecoveryError('INVALID_SOURCE')
            self._sources[source.connection] = source
            self._catalog.clear()

    def _entries(self):
        entries = {}
        for connection, source in self._sources.items():
            rows = source.records()
            for row in rows:
                if (not isinstance(row, dict) or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', ''))
                        or type(row.get('size')) is not int or row['size'] <= 0):
                    raise RecoveryError('INVALID_SOURCE')
                row = copy.deepcopy(row)
                binding = [self.profile_id, connection, source.generation, row]
                identifier = hashlib.sha256(_encoded(binding)).hexdigest()
                entries[identifier] = (source, row)
                if len(entries) > self._max_candidates:
                    raise RecoveryError('INVALID_SOURCE')
        return entries

    def catalog(self, profile_id, activation):
        with self._lock:
            self._fence(profile_id, activation)
            try:
                self._catalog = self._entries()
                return [dict(id=k, size=r['size'], sha256=r['sha256'],
                             complete=_completion(r).get('complete'))
                        for k, (_, r) in sorted(self._catalog.items())]
            except Exception:
                self._catalog.clear()
                raise RecoveryError('INVALID_SOURCE') from None

    def allocate_destination(self, profile_id, activation):
        """Allocate ONLY an excluded, private child; no renderer path or boolean."""
        with self._lock, ExitStack() as stack:
            self._fence(profile_id, activation)
            root = self.recovered_root
            if not core.excluded(root.relative_to(self.profile).as_posix()):
                raise RecoveryError('INVALID_DESTINATION')
            parent = root / uuid.uuid4().hex
            if os.name == 'nt':
                for p in (self.profile, self.profile/'cache', self.profile/'cache/finix', root, parent):
                    core.private_dir(p)
                    stack.enter_context(core.locked(p))
            else:
                _PosixAnchor(stack, parent, owned_from=self.profile, private_from=root, create=True)
            identities = tuple((p, _identity(p.stat(follow_symlinks=False)))
                               for p in (self.profile, self.profile/'cache', self.profile/'cache/finix', root, parent))
            capability = _ManagedDestination(parent/'bundle', identities)
            self._managed.add(capability)
            self._destination(capability)
            return capability

    def _managed_protected(self, capability):
        # Broad host profile ancestors may enclose the exception. Any narrower
        # protected data/output subtree remains forbidden, including other jobs.
        if capability not in self._managed:
            raise RecoveryError('INVALID_DESTINATION')
        return [p for p in self.protected if not (p == self.profile or p in self.profile.parents)]

    def is_managed_result(self, path):
        with self._lock:
            capability = next((c for c in self._managed if c.path == path),None)
            receipt = next((h for h in self._handoffs.values() if h['bundle_root'] == str(path)),None)
            if capability is None or receipt is None:
                return False
            with self._managed_guard(capability):
                for p in (path,Path(receipt['profile_root'])):
                    if os.name == 'nt':
                        core.private_dir(p)
                    else:
                        with ExitStack() as stack:
                            _PosixAnchor(stack,p,owned_from=self.profile,private_from=self.recovered_root)
                if (_identity(path.stat(follow_symlinks=False)) != receipt['bundle_identity']
                        or _identity(Path(receipt['profile_root']).stat(follow_symlinks=False)) != receipt['profile_identity']):
                    raise RecoveryError('INVALID_DESTINATION')
                return True

    def cleanup_managed_result(self, identifier):
        """Erase only the exact identity-registered handoff, never an HTTP path."""
        with self._lock:
            receipt = self._handoffs.get(identifier)
            if receipt is None:
                return
            path = Path(receipt['bundle_root'])
            capability = next((c for c in self._managed if c.path == path),None)
            if capability is None:
                raise RecoveryError('INVALID_DESTINATION')
            with self._managed_guard(capability):
                self.is_managed_result(path)
                if os.name == 'nt':
                    from . import windows_native as win
                    dispose = win.api(win.K,'SetFileInformationByHandle',win.W.BOOL,
                                      win.W.HANDLE,win.C.c_int,win.C.c_void_p,win.W.DWORD)
                    def erase(parent,name,directory_path,is_directory):
                        handle = win.relative_open(parent,name,directory=is_directory,delete=True)
                        try:
                            if directory_path == path and _identity(path.stat(follow_symlinks=False)) != receipt['bundle_identity']:
                                raise RecoveryError('INVALID_DESTINATION')
                            if is_directory:
                                for child in directory_path.iterdir():
                                    info = child.stat(follow_symlinks=False)
                                    erase(handle,child.name,child,stat.S_ISDIR(info.st_mode))
                            flag = win.W.BOOL(True)
                            if not dispose(handle,4,win.C.byref(flag),win.C.sizeof(flag)):
                                raise win.error('Cannot erase managed recovery component')
                        finally:
                            win.close(handle)
                    with win.directory(path.parent) as parent:
                        erase(parent.handle,path.name,path,True)
                else:
                    with ExitStack() as stack:
                        parent = _PosixAnchor(stack,path.parent,owned_from=self.profile,private_from=self.recovered_root)
                        fd = os.open(path.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent.fd)
                        stack.callback(os.close,fd)
                        if _identity(os.fstat(fd)) != receipt['bundle_identity']:
                            raise RecoveryError('INVALID_DESTINATION')
                        _erase_directory(fd)
                        parent.check()
                        if _identity(os.stat(path.name,dir_fd=parent.fd,follow_symlinks=False)) != receipt['bundle_identity']:
                            raise RecoveryError('INVALID_DESTINATION')
                        os.rmdir(path.name,dir_fd=parent.fd)
            self._handoffs.pop(identifier,None)
            self._managed.discard(capability)
            # Host-only retirement evidence for the registered completion owner.
            # No receipt is returned for an absent/already-retired handoff.
            return copy.deepcopy(receipt)

    @contextmanager
    def managed_result_guard(self, path):
        """Keep allocated ancestor handles alive through supported host import."""
        capability = next((c for c in self._managed if c.path == path),None)
        if capability is None:
            yield
        else:
            with self._managed_guard(capability):
                self.is_managed_result(path)
                yield
                self.is_managed_result(path)

    @contextmanager
    def _managed_guard(self, capability):
        protected = self._managed_protected(capability)
        path = core.checked_path(capability.path)
        root = self.recovered_root
        if (path.parent.parent != root or path.name != 'bundle'
                or not re.fullmatch('[a-f0-9]{32}',path.parent.name)
                or any(_overlap(path,p) for p in [self.stage_root,*protected])):
            raise RecoveryError('INVALID_DESTINATION')
        with ExitStack() as stack:
            anchor = None
            if os.name == 'nt':
                for p, identity in capability.identities:
                    stack.enter_context(core.locked(p))
                    core.private_dir(p)
            else:
                anchor = _PosixAnchor(stack,path.parent,owned_from=self.profile,private_from=root)
            def check():
                if anchor is not None:
                    anchor.check()
                for p, identity in capability.identities:
                    if _identity(p.stat(follow_symlinks=False)) != identity:
                        raise RecoveryError('INVALID_DESTINATION')
            check()
            yield path
            check()

    def _destination(self, destination):
        if isinstance(destination, _ManagedDestination):
            with self._managed_guard(destination) as path:
                if path.exists() and (not path.is_dir() or any(path.iterdir())):
                    raise RecoveryError('INVALID_DESTINATION')
                return path
        path = core.checked_path(destination)
        if (path == Path(path.anchor) or any(_overlap(path, p) for p in self.protected)
                or not path.parent.is_dir() or (path.exists() and
                    (not path.is_dir() or any(path.iterdir())))):
            raise RecoveryError('INVALID_DESTINATION')
        return path

    def start(self, candidate_id, *, identity, destination, profile_id, activation):
        """Internal only: identity path/stream and destination MUST be host-authorized."""
        with self._lock:
            self._fence(profile_id, activation)
            if self._worker and self._worker.is_alive():
                raise RecoveryError('BUSY')
            try:
                validated = self._destination(destination)
                if not isinstance(destination, _ManagedDestination):
                    destination = validated
            except Exception:
                raise RecoveryError('INVALID_DESTINATION') from None
            if candidate_id not in self._catalog:
                raise RecoveryError('INVALID_CANDIDATE')
            identifier = uuid.uuid4().hex
            while len(self._jobs) >= self._max_jobs:
                oldest = next(iter(self._jobs))
                self._handoffs.pop(oldest, None)
                del self._jobs[oldest]
            job = dict(id=identifier, status='running', phase='preparing', bytes=0,
                       error=None, result=None)
            event = threading.Event()
            self._jobs[identifier] = (job, event)
            epoch = self._epoch
            source, row = self._catalog[candidate_id]
            dispatch_context = copy_context()
            self._worker = threading.Thread(target=dispatch_context.run,
                args=(self._run, identifier, epoch, candidate_id, source, row, identity, destination),
                name='finix-recovery', daemon=False)
            self._worker.start()
            return identifier

    def _run(self, identifier, epoch, candidate_id, source, row, identity, destination):
        stage = None
        operation = ExitStack()
        job, event = self._jobs[identifier]
        def cancelled():
            return event.is_set() or self._closed or epoch != self._epoch
        def check():
            if cancelled():
                raise RecoveryError('CANCELLED')
            self._check_authority()
        def progress(value):
            with self._lock:
                if not cancelled() and job['status'] == 'running':
                    job['phase'] = 'restoring'
                    n = value.get('bytes', 0)
                    job['bytes'] = n if type(n) is int and n >= 0 else 0
        try:
            if self._authority_guard is not None:
                operation.enter_context(self._authority_guard.lease())
            check()
            if candidate_id not in self._entries():
                raise RecoveryError('INVALID_CANDIDATE')
            with _stage_area(self) as (stage, check_stage):
                key = stage / 'identity'
                check()
                with core.regular_output(key) as fd:
                    if hasattr(identity, 'read'):
                        data = identity.read(65537)
                    else:
                        with core.regular_input(core.checked_path(identity)) as sourcefd:
                            data = core.read_regular(sourcefd, 65536)
                    if not isinstance(data, bytes) or len(data) > 65536:
                        raise RecoveryError('INVALID_IDENTITY')
                    try:
                        # Desktop exports UTF-8 comments; only the identity is ASCII.
                        # Accept one leading UTF-8 BOM for saved text-file compatibility.
                        text = data.decode('utf-8-sig')
                    except UnicodeDecodeError:
                        raise RecoveryError('INVALID_IDENTITY') from None
                    lines = [line.strip() for line in text.splitlines()
                             if line.strip() and not line.lstrip().startswith('#')]
                    if len(lines) != 1 or not re.fullmatch(r'AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}', lines[0]):
                        raise RecoveryError('INVALID_IDENTITY')
                    os.write(fd, (lines[0] + '\n').encode('ascii'))
                    del data, text, lines
                identity = None
                cipher = stage / 'snapshot.age'
                check()
                with core.regular_output(cipher) as fd, os.fdopen(os.dup(fd), 'wb') as stream:
                    sink = _VerifiedSink(stream, row['size'], check)
                    def transfer_cancelled():
                        check()
                        return False
                    source.transfer(copy.deepcopy(row), sink, transfer_cancelled)
                    stream.flush()
                    if sink.size != row['size'] or sink.digest.hexdigest() != row['sha256']:
                        raise RecoveryError('INTEGRITY_FAILED')
                check()
                if candidate_id not in self._entries():
                    raise RecoveryError('INVALID_CANDIDATE')
                check_stage()
                result = _restore_anchored(cipher, key, stage / 'bundle', cancel=transfer_cancelled, progress=progress)
                key.unlink()
                check()
                check_stage()
                # Serialize fence/cancel against final publication. No stale publish.
                with self._lock, ExitStack() as anchors:
                    check()
                    capability = destination if isinstance(destination, _ManagedDestination) else None
                    protected = self._managed_protected(capability) if capability else self.protected
                    path = capability.path if capability else destination
                    if os.name == 'nt':
                        expected_parent, protected_pins = None, None
                    else:
                        protected_pins = _pin_protected(anchors, protected)
                        expected_parent = _PosixAnchor(anchors, path.parent,
                            owned_from=self.profile if capability else None,
                            private_from=self.recovered_root if capability else None)
                    destination = self._destination(destination)
                    with _publish_parent(destination, protected,
                            expected_parent=expected_parent, protected_pins=protected_pins) as publish_target:
                        check_stage()
                        if publish_target.exists():
                            with core.locked(publish_target):
                                if any(publish_target.iterdir()):
                                    raise RecoveryError('INVALID_DESTINATION')
                            # Windows no-delete-share guard must be released;
                            # atomic rmdir refuses any raced-in content.
                            publish_target.rmdir()
                        check()
                        core.publish_directory(stage / 'bundle', publish_target)
                        pinned_profile = publish_target / 'profile' if result.get('layout') == 'bundle' else publish_target
                        config_present = (pinned_profile / 'config.yaml').is_file()
                        bundle_identity = _identity(publish_target.stat(follow_symlinks=False))
                        profile_identity = _identity(pinned_profile.stat(follow_symlinks=False))
                    profile_root = destination / 'profile' if result.get('layout') == 'bundle' else destination
                    self._handoffs[identifier] = dict(bundle_root=str(destination),
                        bundle_identity=bundle_identity,profile_identity=profile_identity,
                        profile_root=str(profile_root), complete=result.get('complete'),
                        source_profile=copy.deepcopy(result.get('source_profile')),
                        history_payload=copy.deepcopy(result.get('history_payload')),
                        # Separate content integrity pin; deliberately NOT a source
                        # namespace/principal/capture-authority assertion.
                        new_copy_payload=({'size': (profile_root / 'state.db').stat().st_size,
                            'sha256': core.digest_file(profile_root / 'state.db')}
                            if (profile_root / 'state.db').is_file() else None),
                        cipher_sha256=row['sha256'], cipher_size=row['size'],
                        omission_report=result.get('omission_report'),
                        config_present=config_present,
                        cron_disabled=True, activated=False,
                        extras_policy='separate_manual_import_only',
                        activation='requires_host_confirmation',
                        warnings=['HOST_LIFECYCLE_REQUIRED', 'PRESERVE_CURRENT_PROFILE_ROLLBACK',
                                  'REVIEW_RECOVERED_SETTINGS_BEFORE_ACTIVATION'])
                    check()
                    outcome = dict(status='succeeded', phase='complete', result={
                        'activation': 'requires_host_confirmation',
                        'files': result['files'], 'cron_disabled': True,
                        'complete': result.get('complete'),
                        'omitted_count': result.get('omitted_count', 0),
                        'layout': result.get('layout')})
        except Exception as exc:
            with self._lock:
                code = ('RESTORE_FAILED' if isinstance(exc, _CleanupFailure) else
                        'CANCELLED' if cancelled() else
                        exc.code if isinstance(exc, RecoveryError) else 'RESTORE_FAILED')
                outcome = dict(status='cancelled' if code == 'CANCELLED' else 'failed', phase='complete', error=code)
        finally:
            identity = None
            # _stage_area completes private cleanup while its handles are alive,
            # before the job may become terminal. Never reuse the stage pathname.
            with self._lock:
                if outcome['status'] == 'succeeded':
                    try:
                        check()
                    except Exception:
                        outcome = dict(status='failed', phase='complete', error='STALE_PROFILE')
                if outcome['status'] != 'succeeded' and identifier in self._handoffs:
                    try:
                        self.cleanup_managed_result(identifier)
                    except Exception:
                        outcome = dict(status='failed', phase='complete', error='RESTORE_FAILED')
                job.update(outcome)
                if job['status'] != 'succeeded':
                    self._handoffs.pop(identifier, None)
            operation.close()

    def handoff(self, identifier, profile_id, activation):
        """Trusted host-only result; NEVER return these paths in HTTP diagnostics.

        This is a candidate, not activation authorization. Host must confirm with
        the user, preserve rollback, stop gateway/use supported profile lifecycle,
        validate settings/hydration and switch only via its documented API.
        """
        with self._lock:
            self._fence(profile_id, activation)
            if self.status(identifier)['status'] != 'succeeded' or identifier not in self._handoffs:
                raise RecoveryError('NOT_FOUND')
            result = copy.deepcopy(self._handoffs[identifier])
            # Recheck the exact allocated ancestry on every host handoff.
            self.is_managed_result(Path(result['bundle_root']))
            if not core.checked_path(result['profile_root']).is_dir():
                raise RecoveryError('INVALID_DESTINATION')
            return result

    def status(self, identifier):
        with self._lock:
            if identifier not in self._jobs:
                raise RecoveryError('NOT_FOUND')
            return copy.deepcopy(self._jobs[identifier][0])

    def cancel(self, identifier):
        with self._lock:
            if identifier not in self._jobs:
                raise RecoveryError('NOT_FOUND')
            if self._jobs[identifier][0]['status'] == 'running':
                self._jobs[identifier][1].set()
            return self.status(identifier)

    def wait(self, identifier, timeout=None):
        with self._lock:
            worker = self._worker if identifier in self._jobs and self._jobs[identifier][0]['status'] == 'running' else None
        if worker is not None:
            worker.join(timeout)
        return self.status(identifier)

    def deactivate(self):
        """Host MUST call synchronously on profile/activation changes."""
        with self._lock:
            self._epoch += 1
            self._catalog.clear()
            for job, event in self._jobs.values():
                if job['status'] == 'running':
                    event.set()

    def close(self):
        with self._lock:
            self._closed = True
            self.deactivate()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join()


class _VerifiedSink:
    """Streaming ciphertext quota and full digest, verified before decryption."""
    def __init__(self, stream, expected, check):
        self.stream, self.expected, self.check = stream, expected, check
        self.size, self.digest = 0, hashlib.sha256()

    def write(self, data):
        self.check()
        if self.size + len(data) > self.expected:
            raise RecoveryError('INTEGRITY_FAILED')
        self.stream.write(data)
        self.digest.update(data)
        self.size += len(data)
        return len(data)
