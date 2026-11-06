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
    from .core import FinixError
    for suffix in ('-wal', '-shm', '-journal'):
        try:
            info = os.stat(name + suffix, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise FinixError('SQLite companion is a symlink or special file')
    path = f'/proc/self/fd/{parent}/{name}'
    before = os.stat(path)
    pinned = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise FinixError('SQLite source replaced')
    connection = sqlite3.connect(f'file:{quote(path, safe="/")}?mode=ro', uri=True, timeout=2)
    try:
        # Pin one read view so concurrent WAL writers cannot restart the backup forever.
        connection.execute('BEGIN')
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return connection
    except BaseException:
        connection.close()
        raise


def _sqlite_backup(source, destination, cancel=None, progress=None, memory_limit=None):
    from .core import FinixError
    page_size = source.execute('pragma page_size').fetchone()[0]
    destination.execute('pragma cache_size=-2048')
    destination.execute('pragma temp_store=MEMORY')
    def on_progress(status, remaining, total):
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


def _add_payload(archive, name, stream, size, records, cancel, progress):
    from .core import FinixError
    info = tarfile.TarInfo('profile/' + name)
    info.mode, info.size = 0o600, size
    reader = HashReader(stream, cancel, progress)
    archive.addfile(info, reader)
    if reader.count != size:
        raise FinixError('Source file shortened while streaming')
    records[name] = {'size': size, 'sha256': reader.hash.hexdigest()}


def write_archive(rootfd, output, sqlite_temp_dir=None, cancel=None, progress=None):
    from . import core
    inventory = core.scan(rootfd)
    records, companions = {}, set()
    with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0, compresslevel=1) as zipped:
        # GNU base-256 headers support files >=8 GiB without a size ceiling.
        with tarfile.open(fileobj=zipped, mode='w|', format=tarfile.GNU_FORMAT) as archive:
            for name, expected in inventory.items():
                check_cancel(cancel)
                if name in companions:
                    continue
                with core.source_file(rootfd, name, expected) as (fd, parent, leaf):
                    if os.pread(fd, 16, 0) == b'SQLite format 3\x00':
                        companions.update(name + s for s in ('-wal', '-shm', '-journal'))
                        with sqlite_payload(fd, parent, leaf, sqlite_temp_dir, cancel, progress) as (stream, size):
                            _add_payload(archive, name, stream, size, records, cancel, progress)
                    elif name == 'cron/jobs.json':
                        data = core.normalize_cron(core.read_regular(fd, CRON_BYTES))
                        if len(data) > CRON_BYTES:
                            raise core.FinixError('Cron JSON exceeds metadata limit')
                        _add_payload(archive, name, io.BytesIO(data), len(data), records, cancel, progress)
                    else:
                        with os.fdopen(os.dup(fd), 'rb') as stream:
                            _add_payload(archive, name, stream, expected[3], records, cancel, progress)
            final = core.scan(rootfd)
            for name in companions & inventory.keys() & final.keys():
                if inventory[name][:3] != final[name][:3]:
                    raise core.FinixError('SQLite companion replaced during snapshot')
            if ({n: s for n, s in inventory.items() if n not in companions}
                    != {n: s for n, s in final.items() if n not in companions}):
                raise core.FinixError('Profile changed during snapshot; retry when quiescent')
            manifest = json.dumps({'format': 'finix-2', 'files': records}, sort_keys=True).encode()
            if len(manifest) > MANIFEST_BYTES:
                raise core.FinixError('Manifest exceeds metadata limit')
            info = tarfile.TarInfo('manifest.json')
            info.size, info.mode = len(manifest), 0o600
            archive.addfile(info, io.BytesIO(manifest))


class Pipe:
    """Cancellable bounded pipe I/O, with no fixed short wall-clock deadline."""
    def __init__(self, stream, cancel):
        self.stream, self.cancel = stream, cancel
        os.set_blocking(stream.fileno(), False)

    def _ready(self, event):
        with selectors.DefaultSelector() as selector:
            selector.register(self.stream, event)
            while True:
                check_cancel(self.cancel)
                if selector.select(0.2):
                    return

    def write(self, data):
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
    if process.poll() is None:
        process.kill()
    process.wait()
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


def write_encrypted_snapshot(rootfd, recipient, output, sqlite_temp_dir=None, cancel=None, progress=None):
    from .core import FinixError
    try:
        process = subprocess.Popen(['age', '-r', recipient], stdin=subprocess.PIPE,
                                   stdout=output, stderr=subprocess.DEVNULL)
        try:
            assert process.stdin is not None
            write_archive(rootfd, Pipe(process.stdin, cancel), sqlite_temp_dir, cancel, progress)
            process.stdin.close()
            _wait(process, cancel)
        finally:
            _cleanup(process)
    except OSError:
        raise FinixError('Streaming encrypted snapshot failed; check age and available storage') from None


@contextmanager
def decrypted_stream(path, identity, cancel=None):
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
        lines = [line.strip() for line in data.decode('ascii').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        if (len(lines) != 1 or not core.re.fullmatch(
                r'AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}', lines[0])):
            raise core.FinixError('Only one native age identity is supported; no plugins')
        keyfd = os.memfd_create('finix-identity', os.MFD_CLOEXEC)
        os.write(keyfd, data)
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
    try:
        with gzip.GzipFile(fileobj=stream, mode='rb') as zipped:
            for info in _headers(zipped, cancel):
                name = info.name
                candidate = name.removeprefix('profile/')
                if (name in names or len(names) >= core.MAX_FILES + 1
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
        if version == 'finix-2':
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
        for name in records:
            if any(str(parent) in records for parent in PurePosixPath(name).parents):
                raise core.FinixError('Archive file/directory conflict')
        payload = stage / 'profile' if version == 'finix-2' else stage
        payload.mkdir(mode=0o700, exist_ok=True)
        _pause_cron(payload)
        return payload, len(records)
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError,
            tarfile.TarError, OSError, EOFError, zlib.error):
        raise core.FinixError('Invalid or incomplete archive; check free restore space') from None
