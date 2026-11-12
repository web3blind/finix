"""Streaming tar/gzip/age snapshots without an archive-size ceiling.

Owner-approved SQLite online-backup files are private (0700 directory, 0600
files) and removed after success/error/cancellation. They are NOT encrypted;
crash leftovers may survive, and unlink is not secure erasure. Whole plaintext
archives never spool to disk; only the final age ciphertext is persisted.
"""
from contextlib import contextmanager
from urllib.parse import quote
import gzip
import zlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile

CHUNK = 1024 * 1024
RAM_SQLITE_BYTES = 16 * 1024 * 1024
MANIFEST_BYTES = 8 * 1024 * 1024
CRON_BYTES = 4 * 1024 * 1024


def check_cancel(cancel):
    from .preview_budget import check
    check()
    from .core import FinixError
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise FinixError('Operation cancelled')


def report(progress, phase, count):
    if progress is not None:
        progress({'phase': phase, 'bytes': count})


def _temp_directory(path):
    from .core import checked_path
    return checked_path(path if path is not None else tempfile.gettempdir())


def _safe_temp_parent(info):
    owned = info.st_uid == os.geteuid()
    private_writes = not info.st_mode & 0o022
    # A conventional root-owned sticky /tmp is safe for private mkdtemp children.
    sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    return stat.S_ISDIR(info.st_mode) and ((owned and private_writes) or sticky_root)


def sqlite_temp_readiness(path):
    if os.name == 'nt':
        from .windows_native import temp_readiness
        return temp_readiness(_temp_directory(path))
    from .core import FinixError
    failure = {'ready': False, 'code': 'NEED_SQLITE_TEMP_DIRECTORY', 'message':
        'Set sqlite_temp_dir to an existing writable directory owned by this '
        'user without group/world write access (or a root-owned sticky temporary '
        'directory). SQLite snapshots are temporary plaintext files, not encrypted.'}
    try:
        directory = _temp_directory(path)
        info = directory.stat()
        if not _safe_temp_parent(info) or not os.access(directory, os.W_OK | os.X_OK):
            return failure
        return {'ready': True, 'code': 'READY', 'message':
                'SQLite snapshots use private temporary plaintext files, removed '
                'on success/error/cancellation; crash leftovers may remain.',
                'free_bytes': shutil.disk_usage(directory).free}
    except (FinixError, OSError, ValueError, RuntimeError):
        return failure


@contextmanager
def sqlite_stage(path, required_bytes):
    if os.name == 'nt':
        from .windows_native import sqlite_stage as native_stage
        with native_stage(_temp_directory(path), required_bytes) as stage:
            yield stage
        return
    from .core import FinixError
    readiness = sqlite_temp_readiness(path)
    if not readiness['ready']:
        raise FinixError(readiness['code'] + ': ' + readiness['message'])
    directory = _temp_directory(path)
    parentfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stage = None
    try:
        if not _safe_temp_parent(os.fstat(parentfd)):
            raise FinixError('NEED_SQLITE_TEMP_DIRECTORY: temporary directory changed')
        pinned = Path(f'/proc/self/fd/{parentfd}')
        free = shutil.disk_usage(pinned).free
        if free < required_bytes:
            raise FinixError(f'NOT_ENOUGH_SQLITE_TEMP_SPACE: need {required_bytes} bytes, '
                             f'available {free}; choose sqlite_temp_dir on a larger filesystem')
        stage = Path(tempfile.mkdtemp(prefix='.finix-sqlite-', dir=pinned))
        os.chmod(stage, 0o700)
        yield stage
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        os.close(parentfd)


def _connect_source(fd, parent, name):
    if os.name == 'nt':
        from .windows_native import connect_source
        return connect_source(fd, parent, name)
    from .core import FinixError
    pinned_companions = {}
    def pin_companions():
        for suffix in ('-wal', '-shm', '-journal'):
            leaf = name + suffix
            if leaf in pinned_companions:
                continue
            try:
                companion = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                continue
            info = os.fstat(companion)
            if not stat.S_ISREG(info.st_mode):
                os.close(companion)
                raise FinixError('SQLite companion is a symlink or special file')
            pinned_companions[leaf] = (companion, (info.st_dev, info.st_ino, info.st_mode))
    def verify_companions():
        for leaf, (_, identity) in pinned_companions.items():
            try:
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                raise FinixError('SQLite companion replaced during snapshot') from None
            if (info.st_dev, info.st_ino, info.st_mode) != identity:
                raise FinixError('SQLite companion replaced during snapshot')
    def release():
        for companion, _ in pinned_companions.values():
            os.close(companion)
        pinned_companions.clear()
    class PinnedConnection(sqlite3.Connection):
        def close(self):
            try:
                verify_companions()
            finally:
                try:
                    super().close()
                finally:
                    release()
    connection = None
    try:
        pin_companions()
        path = f'/proc/self/fd/{parent}/{name}'
        before = os.stat(path, follow_symlinks=False)
        pinned = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_mode) != (pinned.st_dev, pinned.st_ino, pinned.st_mode):
            raise FinixError('SQLite source replaced')
        connection = sqlite3.connect(f'file:{quote(path, safe="/")}?mode=ro', uri=True,
                                     timeout=2, factory=PinnedConnection)
        verify_companions()
        # Pin one read view so concurrent WAL writers cannot restart the backup forever.
        connection.execute('BEGIN')
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
        pin_companions()  # SQLite may have created SHM on opening the read view.
        verify_companions()
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        else:
            release()
        raise


def _sqlite_backup(source, destination, cancel=None, progress=None, memory_limit=None):
    from .core import FinixError
    page_size = source.execute('pragma page_size').fetchone()[0]
    destination.execute('pragma cache_size=-2048')
    destination.execute('pragma temp_store=MEMORY')
    def on_progress(status, remaining, total):
        from .preview_budget import check as check_preview_budget
        check_preview_budget()
        check_cancel(cancel)
        if memory_limit is not None and total * page_size > memory_limit:
            raise FinixError('SQLite grew beyond RAM optimization; use the production disk snapshot API')
        report(progress, 'sqlite', (total - remaining) * page_size)
    source.backup(destination, pages=128, progress=on_progress, sleep=0.05)


def memory_sqlite_snapshot(fd, parent, name):
    from .core import FinixError
    source = _connect_source(fd, parent, name)
    destination = sqlite3.connect(':memory:')
    try:
        size = source.execute('pragma page_size').fetchone()[0]
        destination.execute(f'pragma max_page_count={RAM_SQLITE_BYTES // size}')
        _sqlite_backup(source, destination, memory_limit=RAM_SQLITE_BYTES)
        return destination.serialize()
    except sqlite3.Error:
        raise FinixError('SQLite snapshot failed; use the production disk snapshot API for large databases') from None
    finally:
        source.close()
        destination.close()


@contextmanager
def sqlite_payload(fd, parent, name, sqlite_temp_dir, cancel, progress):
    from . import core
    source = _connect_source(fd, parent, name)
    try:
        size = (source.execute('pragma page_count').fetchone()[0]
                * source.execute('pragma page_size').fetchone()[0])
        check_cancel(cancel)
        with sqlite_stage(sqlite_temp_dir, size) as stage:
            snapshot = stage / 'snapshot.sqlite'
            filefd = os.open(snapshot, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(filefd)
            destination = sqlite3.connect(snapshot)
            try:
                destination.execute('pragma journal_mode=OFF')
                _sqlite_backup(source, destination, cancel, progress)
            finally:
                destination.close()
            with snapshot.open('rb') as stream:
                yield stream, os.fstat(stream.fileno()).st_size
    except (sqlite3.Error, OSError):
        raise core.FinixError('SQLite consistent snapshot failed; check temporary storage free space and database health') from None
    finally:
        source.close()


class HashReader:
    def __init__(self, stream, cancel, progress):
        self.stream, self.cancel, self.progress = stream, cancel, progress
        self.hash = hashlib.sha256()
        self.count = 0

    def read(self, size):
        check_cancel(self.cancel)
        data = self.stream.read(min(size, CHUNK))
        self.hash.update(data)
        self.count += len(data)
        report(self.progress, 'snapshot', self.count)
        return data


def _add_payload(archive, name, stream, size, records, cancel, progress, *, raw=False):
    from .core import FinixError
    from . import core
    if not core.safe_name(name):
        raise FinixError('Unsafe archive path or path depth exceeds limit')
    info = tarfile.TarInfo(name if raw else 'profile/' + name)
    info.mode, info.size = 0o600, size
    reader = HashReader(stream, cancel, progress)
    archive.addfile(info, reader)
    if reader.count != size:
        raise FinixError('Source file shortened while streaming')
    records[name] = {'size': size, 'sha256': reader.hash.hexdigest()}


CAPTURE_MEMORY_BYTES = 8 * 1024 * 1024
CAPTURE_ATTEMPTS = 3


@contextmanager
def captured_source(rootfd, name, expected):
    """Pin and classify the generation actually captured, never promote by suffix."""
    from . import core
    from contextlib import ExitStack
    with ExitStack() as opened:
        try:
            fd, parent, leaf = opened.enter_context(core.source_file(rootfd, name, expected))
        except FileNotFoundError:
            if getattr(expected, 'opaque', None) is not None:
                raise core.FinixError('CUSTODY_CHANGED') from None
            if getattr(expected, 'sqlite', False):
                raise core.FinixError('SOURCE_UNAVAILABLE_BEFORE_CAPTURE') from None
            raise core.SourceCaptureError('LIVE_SOURCE_MISSING') from None
        if getattr(expected, 'sqlite', False):
            yield fd, parent, leaf, expected
        else:
            current = core.discovered_source(fd, parent, leaf, relative=name)
            if getattr(current,'opaque',None)!=getattr(expected,'opaque',None):
                raise core.FinixError('CUSTODY_CHANGED')
            with core.validated_source(fd, parent, leaf, current):
                yield fd, parent, leaf, current


def capture_identity(fd, parent, leaf):
    """Path and handle must agree during copy; never follow a replacement link."""
    from . import core
    import stat
    observed = core.fingerprint(os.fstat(fd))
    if not stat.S_ISREG(observed[2]):
        raise core.FinixError('SOURCE_UNSAFE_DURING_CAPTURE')
    if os.name != 'nt':
        try:
            current = core.fingerprint(os.stat(leaf, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            raise core.FinixError('SOURCE_REPLACED_DURING_CAPTURE') from None
        if current[:3] != observed[:3]:
            raise core.FinixError('SOURCE_REPLACED_DURING_CAPTURE')
    return observed


@contextmanager
def ordinary_payload(fd, parent, leaf, sqlite_temp_dir=None, cancel=None, *, memory_limit=CAPTURE_MEMORY_BYTES, opaque=None):
    """Capture once, then hash/archive only the private captured copy.

    Large ordinary files use only the configured owner-private SQLite staging
    area. No OS-temp plaintext fallback. Retry in-place metadata/content races
    a bounded number of times; identity/type substitution is never retried.
    """
    from . import core
    from contextlib import ExitStack
    disk = os.fstat(fd).st_size > memory_limit
    for attempt in range(CAPTURE_ATTEMPTS):
        check_cancel(cancel)
        with ExitStack() as stack:
            before = capture_identity(fd, parent, leaf)
            if disk:
                if sqlite_temp_dir is None:
                    raise core.FinixError('NEED_SQLITE_TEMP_DIRECTORY')
                stage = stack.enter_context(sqlite_stage(sqlite_temp_dir, before[3]))
                path = stage / 'ordinary.snapshot'
                target = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                stream = stack.enter_context(os.fdopen(target, 'w+b'))
            else:
                stream = stack.enter_context(io.BytesIO())
            os.lseek(fd, 0, os.SEEK_SET)
            size = 0
            raw_digest = hashlib.sha256() if opaque else None
            header = b''
            overflow = False
            remaining = before[3] + 1  # Bound even a writer that appends forever.
            while remaining:
                check_cancel(cancel)
                chunk = os.read(fd, min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                if not size:
                    header = chunk[:16]
                size += len(chunk)
                if raw_digest is not None: raw_digest.update(chunk)
                if not disk and size > memory_limit:
                    disk, overflow = True, True
                    break
                stream.write(chunk)
            after = capture_identity(fd, parent, leaf)
            if after[:3] != before[:3]:
                raise core.FinixError('SOURCE_REPLACED_DURING_CAPTURE')
            if header == b'SQLite format 3\x00' or core.peek(fd, 16) == b'SQLite format 3\x00':
                raise core.FinixError('SOURCE_CLASSIFICATION_CHANGED_DURING_CAPTURE')
            if not overflow and before == after and size == before[3]:
                if opaque is not None and (size!=opaque['size'] or raw_digest.hexdigest()!=opaque['sha256']):
                    raise core.FinixError('CUSTODY_CHANGED')
                stream.seek(0)
                yield stream, size
                return  # Later source generations cannot invalidate captured bytes.
    if opaque is not None:
        raise core.FinixError('CUSTODY_CHANGED')
    raise core.SourceCaptureError('SOURCE_UNSTABLE_CAPTURE')


@contextmanager
def file_payload(rootfd, name, expected, sqlite_temp_dir=None, cancel=None, progress=None):
    """Release ordinary source handles as soon as the private copy is complete."""
    from contextlib import ExitStack
    with ExitStack() as copies:
        with captured_source(rootfd, name, expected) as (fd, parent, leaf, captured):
            if captured.sqlite:
                with sqlite_payload(fd, parent, leaf, sqlite_temp_dir, cancel, progress) as (stream, size):
                    yield stream, size, captured
                return
            options={'opaque':captured.opaque} if getattr(captured,'opaque',None) is not None else {}
            stream, size = copies.enter_context(ordinary_payload(fd, parent, leaf, sqlite_temp_dir, cancel, **options))
        # The copied stream no longer depends on the original source path/fd.
        yield stream, size, captured


def _source_namespace(rootfd):
    """Resolve the actual retained capture home through the installed SDK.

    This is namespace metadata, never sender/principal authentication. Unknown
    non-SDK sources remain readable archives but cannot authorize history rekey.
    """
    from . import core
    try:
        from hermes_constants import profile_name_for_home
        from hermes_cli.profiles import get_profile_dir, validate_profile_name
    except ImportError:
        return None
    if os.name == 'nt':
        from .windows_native import canonical_directory
        home, identity = canonical_directory(rootfd.path)
        if identity != rootfd.identity():
            raise core.FinixError('HISTORY_SOURCE_BINDING_REQUIRED')
    else:
        home = Path(os.readlink('/proc/self/fd/' + str(rootfd)))
        info = os.fstat(rootfd)
        identity = (info.st_dev, info.st_ino)
    name = profile_name_for_home(home)
    if name is None:
        return None
    try:
        validate_profile_name(name)  # exact SDK result, never case repair
        resolved = get_profile_dir(name)
        if os.name == 'nt':
            if canonical_directory(resolved)[1] != identity:
                raise ValueError()
        else:
            fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != identity:
                    raise ValueError()
            finally:
                os.close(fd)
    except (ValueError, OSError):
        raise core.FinixError('HISTORY_SOURCE_BINDING_REQUIRED') from None
    return name


def _profile_stamp(namespace, files):
    return {'version': 1, 'namespace': namespace,
            'files_sha256': hashlib.sha256(json.dumps(files, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()}


def _verified_profile_stamp(manifest):
    from . import core
    stamp = manifest.get('source_profile')
    if stamp is None:
        return None  # frozen old manifest compatibility; not migration authority
    if (not isinstance(stamp, dict) or set(stamp) != {'version', 'namespace', 'files_sha256'}
            or type(stamp['version']) is not int or stamp['version'] != 1
            or not isinstance(stamp['namespace'], str)
            or not core.re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', stamp['namespace'])
            or stamp != _profile_stamp(stamp['namespace'], manifest['files'])):
        raise core.FinixError('HISTORY_SOURCE_BINDING_REQUIRED')
    return dict(stamp)


def write_archive(rootfd, output, sqlite_temp_dir=None, cancel=None, progress=None, *, extra_fds=()):
    from . import core
    namespace = _source_namespace(rootfd)
    from contextlib import ExitStack
    from .extra_sources import guard_tree
    from functools import partial
    guards = {fd: partial(guard_tree, root_name=Path(item['path']).name) for item, fd in extra_fds}

    def scan_source(fd):
        return core.scan(fd, guards.get(fd))

    roots = [('', rootfd), *[('external/' + item['name'] + '/', fd) for item, fd in extra_fds]]
    inventories = [(prefix, fd, scan_source(fd)) for prefix, fd in roots]
    records, checks, omitted = {}, [], []
    completed_bytes = completed_files = 0
    # Discovery sizes may change during stable per-file capture (notably SQLite
    # and normalized cron); do not advertise a misleading fixed byte total.
    def snapshot_progress(event):
        if progress is None: return
        if event.get('phase') == 'snapshot':
            progress({'phase': 'snapshot', 'completed_bytes': completed_bytes + event.get('bytes', 0),
                      'bytes': completed_bytes + event.get('bytes', 0),
                      'completed_files': completed_files, 'total_bytes': None,
                      'total_files': None, 'percent': None})
        else:
            progress(event)

    if progress is not None:
        progress({'phase': 'snapshot', 'bytes': 0, 'completed_bytes': 0, 'completed_files': 0,
                  'total_bytes': None, 'total_files': None, 'percent': None})
    with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0, compresslevel=1) as zipped:
        with tarfile.open(fileobj=zipped, mode='w|', format=tarfile.GNU_FORMAT) as archive:
            for prefix, rootfd, inventory in inventories:
                companions, databases = set(), set()
                for name, expected in inventory.items():
                    check_cancel(cancel)
                    if name in companions:
                        continue
                    with ExitStack() as payload:
                        try:
                            stream, size, captured = payload.enter_context(file_payload(
                                rootfd, name, expected, sqlite_temp_dir, cancel, progress))
                        except core.SourceCaptureError as exc:
                            omitted.append({'path': (('profile/' if not prefix else prefix) + name)
                                            if extra_fds else name, 'code': exc.code})
                            continue
                        if captured.sqlite:
                            inventory[name] = captured
                            databases.add(name)
                            companions.update(name + s for s in ('-wal', '-shm', '-journal'))
                        if not captured.sqlite and prefix == '' and name == 'cron/jobs.json':
                            if size > CRON_BYTES:
                                raise core.FinixError('Cron JSON exceeds metadata limit')
                            data = core.normalize_cron(stream.read(CRON_BYTES + 1))
                            if len(data) > CRON_BYTES:
                                raise core.FinixError('Cron JSON exceeds metadata limit')
                            size = len(data)
                            _add_payload(archive, (('profile/' if not prefix else prefix) + name) if extra_fds else name, io.BytesIO(data), size, records, cancel, snapshot_progress, raw=bool(extra_fds))
                        else:
                            _add_payload(archive, (('profile/' if not prefix else prefix) + name) if extra_fds else name, stream, size, records, cancel, snapshot_progress, raw=bool(extra_fds))
                        completed_bytes += size
                        completed_files += 1
                        snapshot_progress({'phase': 'snapshot', 'bytes': 0})
                core.check_inventory(inventory, inventory, companions, databases, rootfd)
                checks.append((rootfd, inventory, companions, databases))
            for fd, inventory, companions, databases in checks:
                core.check_inventory(inventory, inventory, companions, databases, fd)
            manifest = {'format': 'finix-3' if extra_fds else 'finix-2', 'files': records,
                        'complete': not omitted, 'omitted': omitted}
            if namespace is not None:
                if _source_namespace(rootfd) != namespace:
                    raise core.FinixError('HISTORY_SOURCE_BINDING_REQUIRED')
                manifest['source_profile'] = _profile_stamp(namespace, records)
            if extra_fds:
                manifest['extra_sources'] = [item for item, fd in extra_fds]
            encoded = json.dumps(manifest, sort_keys=True).encode()
            if len(encoded) > MANIFEST_BYTES:
                raise core.FinixError('Manifest exceeds metadata limit')
            info = tarfile.TarInfo('manifest.json')
            info.size, info.mode = len(encoded), 0o600
            archive.addfile(info, io.BytesIO(encoded))
    return {'complete': not omitted, 'omitted_count': len(omitted)}


class Pipe:
    """Cancellable bounded pipe I/O, with no fixed short wall-clock deadline."""
    def __init__(self, stream, cancel):
        self.stream, self.cancel = stream, cancel
        self.native = None
        if os.name == 'nt':
            from .windows_native import Pipe as NativePipe
            self.native = NativePipe(stream, cancel)
            return
        os.set_blocking(stream.fileno(), False)

    def _ready(self, event):
        with selectors.DefaultSelector() as selector:
            selector.register(self.stream, event)
            while True:
                check_cancel(self.cancel)
                if selector.select(0.2):
                    return

    def write(self, data):
        if self.native is not None:
            return self.native.write(data)
        view = memoryview(data)
        count = len(view)
        while view:
            self._ready(selectors.EVENT_WRITE)
            try:
                written = os.write(self.stream.fileno(), view[:CHUNK])
            except BlockingIOError:
                continue
            view = view[written:]
        return count

    def read(self, size=-1):
        if self.native is not None:
            return self.native.read(size)
        from .core import FinixError
        if size < 0:
            raise FinixError('Unbounded pipe read rejected')
        if size == 0:
            return b''
        while True:
            self._ready(selectors.EVENT_READ)
            try:
                return os.read(self.stream.fileno(), min(size, CHUNK))
            except BlockingIOError:
                continue

    def flush(self):
        pass


def _wait(process, cancel):
    from .core import FinixError
    while True:
        check_cancel(cancel)
        try:
            code = process.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            continue
    if code:
        raise FinixError('age rejected the key or encrypted data')


def _cleanup(process):
    if os.name == 'nt':
        from .windows_native import cleanup
        cleanup(process)
    else:
        if process.poll() is None:
            process.kill()
        process.wait()
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


def write_encrypted_snapshot(rootfd, recipient, output, sqlite_temp_dir=None, cancel=None, progress=None, *, extra_fds=()):
    from .core import FinixError
    try:
        if os.name == 'nt':
            from .windows_native import popen
        else:
            popen = subprocess.Popen
        try:
            process = popen(['age', '-r', recipient], stdin=subprocess.PIPE,
                                       stdout=output, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            raise FinixError('DEPENDENCY_UNAVAILABLE') from None
        try:
            assert process.stdin is not None
            outcome = write_archive(rootfd, Pipe(process.stdin, cancel), sqlite_temp_dir, cancel, progress, extra_fds=extra_fds)
            process.stdin.close()
            _wait(process, cancel)
            return outcome
        finally:
            _cleanup(process)
    except OSError:
        raise FinixError('Streaming encrypted snapshot failed; check age and available storage') from None


@contextmanager
def decrypted_stream(path, identity, cancel=None):
    if os.name == 'nt':
        from .windows_native import decrypted_stream as native_decrypt
        with native_decrypt(path, identity, cancel) as stream:
            yield stream
        return
    from . import core
    path, identity = core.checked_path(path), core.checked_path(identity)
    cipherfd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    keyfd, process = None, None
    try:
        if not stat.S_ISREG(os.fstat(cipherfd).st_mode):
            raise core.FinixError('Encrypted input must be a regular file')
        key = os.open(identity, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            data = core.read_regular(key, 65536)
        finally:
            os.close(key)
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise core.FinixError('INVALID_IDENTITY') from None
        lines = [line.strip() for line in text.splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        if (len(lines) != 1 or not core.re.fullmatch(
                r'AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}', lines[0])):
            raise core.FinixError('Only one native age identity is supported; no plugins')
        keyfd = os.memfd_create('finix-identity', os.MFD_CLOEXEC)
        os.write(keyfd, (lines[0] + '\n').encode('ascii'))
        os.lseek(keyfd, 0, os.SEEK_SET)
        process = subprocess.Popen(['age', '-d', '-i', f'/proc/self/fd/{keyfd}'],
            stdin=cipherfd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(keyfd,))
        yield Pipe(process.stdout, cancel)
        _wait(process, cancel)
    except (OSError, UnicodeError):
        raise core.FinixError('Could not stream encrypted archive or native identity') from None
    finally:
        if process is not None:
            _cleanup(process)
        if keyfd is not None:
            os.close(keyfd)
        os.close(cipherfd)


def _exact(stream, size):
    from .core import FinixError
    chunks = []
    remaining = size
    while remaining:
        data = stream.read(min(remaining, CHUNK))
        if not data:
            raise FinixError('Truncated archive')
        chunks.append(data)
        remaining -= len(data)
    return b''.join(chunks)


def _headers(stream, cancel):
    """Parse bounded headers ourselves: TarFile.next allocates untrusted PAX sizes."""
    from .core import FinixError
    longname = None
    while True:
        check_cancel(cancel)
        block = _exact(stream, 512)
        if block == bytes(512):
            if longname is not None or _exact(stream, 512) != bytes(512):
                raise FinixError('Invalid tar end marker')
            while True:
                check_cancel(cancel)
                tail = stream.read(CHUNK)
                if not tail:
                    return
                if tail.strip(b'\x00'):
                    raise FinixError('Nonzero trailing archive data')
        info = tarfile.TarInfo.frombuf(block, 'utf-8', 'surrogateescape')
        if info.type == tarfile.GNUTYPE_LONGNAME:
            if longname is not None or not 0 < info.size <= 65536:
                raise FinixError('Unsupported long archive path')
            longname = _exact(stream, info.size).rstrip(b'\x00').decode('utf-8', 'surrogateescape')
            _exact(stream, (-info.size) % 512)
            continue
        if longname is not None:
            info.name, longname = longname, None
        if not info.isreg() or info.size < 0:
            raise FinixError('Unsafe or unsupported archive entry')
        yield info
        _exact(stream, (-info.size) % 512)


def _restore_file(stage, name, stream, size, cancel, progress):
    if os.name == 'nt':
        from .windows_native import restore_file
        return restore_file(stage, name, stream, size, cancel, progress)
    directory = stage
    for component in PurePosixPath(name).parts[:-1]:
        directory = directory / component
        directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    digest, remaining = hashlib.sha256(), size
    with os.fdopen(fd, 'wb') as target:
        while remaining:
            check_cancel(cancel)
            data = _exact(stream, min(remaining, CHUNK))
            # Preserve long zero regions as holes; their logical bytes are still hashed.
            if data.strip(b'\x00'):
                target.write(data)
            else:
                target.seek(len(data), os.SEEK_CUR)
            digest.update(data)
            remaining -= len(data)
            report(progress, 'restore', size - remaining)
        target.truncate(size)
        target.flush()
        os.fsync(target.fileno())
    return {'size': size, 'sha256': digest.hexdigest()}


def _pause_cron(stage):
    from .core import FinixError
    path = stage / 'cron/jobs.json'
    if not path.exists():
        return
    jobs = json.loads(path.read_bytes())
    if not isinstance(jobs, dict) or not isinstance(jobs.get('jobs'), list):
        raise FinixError('Unsupported cron jobs format')
    for job in jobs['jobs']:
        if not isinstance(job, dict):
            raise FinixError('Unsupported cron job')
        job.update(enabled=False, state='paused', next_run_at=None, paused_at='finix-restore')
    with path.open('wb') as stream:
        stream.write(json.dumps(jobs, indent=2).encode())
        stream.flush()
        os.fsync(stream.fileno())


def restore_stream(stream, stage, cancel=None, progress=None):
    from . import core
    entries, manifest, names = {}, None, set()
    native_names = {}
    try:
        with gzip.GzipFile(fileobj=stream, mode='rb') as zipped:
            for info in _headers(zipped, cancel):
                name = info.name
                if os.name == 'nt':
                    from .windows_native import admit_name
                    admit_name(name)
                    parts = name.split('/')
                    for index in range(1, len(parts) + 1):
                        prefix = '/'.join(parts[:index])
                        key = prefix.upper()
                        if key in native_names and native_names[key] != prefix:
                            raise core.FinixError('Windows archive path alias collision')
                        native_names[key] = prefix
                candidate = name.removeprefix('profile/')
                if (name in names
                        or not (name == 'manifest.json' or core.safe_name(candidate))):
                    raise core.FinixError('Unsafe or duplicate archive entry')
                names.add(name)
                if name == 'manifest.json':
                    if info.size > MANIFEST_BYTES:
                        raise core.FinixError('Manifest exceeds metadata limit')
                    manifest = json.loads(_exact(zipped, info.size))
                    continue
                if candidate == 'cron/jobs.json' and info.size > CRON_BYTES:
                    raise core.FinixError('Cron JSON exceeds metadata limit')
                # Preserve raw member names until the version is known (manifest may be last).
                entries[name] = _restore_file(stage, name, zipped, info.size, cancel, progress)
        if not isinstance(manifest, dict):
            raise core.FinixError('Missing archive manifest')
        version = manifest.get('format')
        if version == 'finix-3':
            from .extra_sources import admit_bundle, manifest_aliases
            aliases = manifest_aliases(manifest)
            records, admit = entries, lambda name: admit_bundle(name, aliases)
        elif version == 'finix-2':
            if any(not n.startswith('profile/') for n in entries):
                raise core.FinixError('Archive scope mismatch')
            records = {n[len('profile/'):]: record for n, record in entries.items()}
            admit = core.allowed
        elif version == 'finix-1':
            records, admit = entries, core.legacy_allowed
        else:
            raise core.FinixError('Unsupported archive format')
        if (not isinstance(manifest.get('files'), dict) or manifest['files'] != records
                or any(not admit(n) for n in records)):
            raise core.FinixError('Archive manifest, checksum or scope mismatch')
        completion = core.validate_completion(manifest, admit)
        stamp = _verified_profile_stamp(manifest)
        if stamp is not None:
            completion['source_profile'] = stamp
            history_key = 'profile/state.db' if version == 'finix-3' else 'state.db'
            completion['history_payload'] = records.get(history_key)
        for name in records:
            if any(str(parent) in records for parent in PurePosixPath(name).parents):
                raise core.FinixError('Archive file/directory conflict')
        payload = stage / 'profile' if version == 'finix-2' else stage
        payload.mkdir(mode=0o700, exist_ok=True)
        _pause_cron(payload / 'profile' if version == 'finix-3' else payload)
        if not completion['complete']:
            # A fresh report name cannot overwrite any archived file/directory.
            import uuid
            while True:
                report_name = 'finix-omissions-' + uuid.uuid4().hex + '.json'
                try:
                    with core.regular_output(payload / report_name) as fd:
                        data = json.dumps({'format': version, **completion, 'omitted': manifest['omitted']}, indent=2).encode()
                        with os.fdopen(os.dup(fd), 'wb') as report_file:
                            report_file.write(data)
                            report_file.flush()
                            os.fsync(report_file.fileno())
                    break
                except FileExistsError:
                    continue
            completion['omission_report'] = report_name
        return payload, len(records), 'bundle' if version == 'finix-3' else 'profile', completion
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError,
            tarfile.TarError, OSError, EOFError, zlib.error):
        raise core.FinixError('Invalid or incomplete archive; check free restore space') from None
