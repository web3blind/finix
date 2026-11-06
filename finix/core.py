"""Bounded-memory, local-only age snapshots. No Hermes runtime imports."""
from contextlib import contextmanager
from urllib.parse import quote
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import selectors
import time
import stat
import subprocess
import tarfile
import tempfile
import uuid

# Compatibility-only buffered helpers; production backup/restore never use this cap.
MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 10000
# Frozen finix-1 admission scope, never used to select new profile backups.
# Executable plugin/skill bytes are inert archive data; restore never imports them.
FILE_ROOTS = frozenset({
    'config.yaml', '.env', 'auth.json', 'auth_state.json', 'state.db',
    'memory_store.db', 'response_store.db', 'projects.db', 'verification_evidence.db',
    'kanban.db', 'gateway_state.json', 'channel_directory.json', 'channel_aliases.json',
    'processes.json', 'feishu_comment_pairing.json',
})
TREE_ROOTS = frozenset({
    'memories', 'skills', 'plugins', 'plugin-data', 'desktop-plugins', 'vault',
    'secrets', 'sessions', 'pairing', 'platforms/pairing', 'kanban/boards',
})
EXACT_FILES = frozenset({
    'cron/jobs.json', 'cron/executions.db', 'gateway/discord_message_recovery.db',
})
# Frozen v1 scope is retained only for backward-compatible archive admission.
LEGACY_PLUGIN_EXCLUSIONS = frozenset({'node_modules', '.git', '__pycache__'})
# Whole-profile policy: exact root trees, plus named regenerable trees anywhere.
# Never filter file extensions or arbitrary project/.git history.
ROOT_EXCLUSIONS = frozenset({'cache', 'logs', 'tmp', 'temp', 'backups', 'profiles', 'hermes-agent'})
TREE_EXCLUSIONS = frozenset({'__pycache__', '.cache', '.pytest_cache', 'node_modules', '.venv', 'venv'})
RECIPIENT = re.compile(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}')


class FinixError(Exception):
    """Safe, non-secret diagnostic suitable for the CLI."""


def excluded(name):
    """Single v2 policy shared by traversal and archive admission."""
    parts = PurePosixPath(name).parts
    return bool(parts) and (parts[0] in ROOT_EXCLUSIONS
                            or any(p in TREE_EXCLUSIONS for p in parts))


def safe_name(name):
    parts = PurePosixPath(name).parts
    return (bool(parts) and len(parts) <= 32 and not name.startswith('/')
            and str(PurePosixPath(name)) == name
            and all(p not in {'.', '..', ''} for p in parts)
            and not any(ord(c) < 32 or c == '\\' for c in name))


def allowed(name):
    return safe_name(name) and not excluded(name)


def legacy_allowed(name):
    parts = PurePosixPath(name).parts
    excluded_plugin = (parts and parts[0] in {'plugins', 'desktop-plugins'}
                       and any(p in LEGACY_PLUGIN_EXCLUSIONS for p in parts[1:]))
    return (safe_name(name) and not excluded_plugin
            and (name in FILE_ROOTS or name in EXACT_FILES
                 or any(name.startswith(root + '/') for root in TREE_ROOTS)))


def checked_path(value):
    path = Path(os.path.abspath(value))
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise FinixError('Symlink paths are not permitted')
    return path


def private_dir(path):
    path = checked_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise FinixError('Expected directory')
    return path


@contextmanager
def locked(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FinixError('Another Finix operation holds the directory lock') from None
        yield fd
    finally:
        os.close(fd)


def fingerprint(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def read_regular(fd, limit=MAX_BYTES):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise FinixError('Not a regular file or size limit exceeded')
    chunks = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b''.join(chunks)
    if len(data) > limit or fingerprint(before) != fingerprint(os.fstat(fd)):
        raise FinixError('File changed while being read or size limit exceeded')
    return data


def scan(rootfd):
    """Open every included component relative to anchored directory descriptors."""
    result = {}
    directories = 0

    def walk(fd, prefix=''):
        nonlocal directories
        directories += 1
        if directories > MAX_FILES or (prefix and len(PurePosixPath(prefix).parts) > 32):
            raise FinixError('Source directory count or depth exceeds limit')
        for name in sorted(os.listdir(fd)):
            relative = f'{prefix}/{name}' if prefix else name
            if excluded(relative):
                continue
            if not allowed(relative):
                raise FinixError('Unsupported source path')
            s = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(s.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if fingerprint(s) != fingerprint(os.fstat(child)):
                        raise FinixError('Source changed during discovery')
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(s.st_mode) and allowed(relative):
                result[relative] = fingerprint(s)
                if len(result) > MAX_FILES:
                    raise FinixError('Too many files')
            else:
                raise FinixError('Included source contains a symlink or special file')
    walk(rootfd)
    return result


@contextmanager
def source_file(rootfd, name, expected):
    parent = os.dup(rootfd)
    fd = None
    try:
        parts = name.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if fingerprint(os.fstat(fd)) != expected:
            raise FinixError('Source changed before read')
        yield fd, parent, parts[-1]
        if fingerprint(os.fstat(fd)) != expected:
            raise FinixError('Source changed during snapshot')
        if fingerprint(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != expected:
            raise FinixError('Source replaced during snapshot')
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def sqlite_snapshot(fd, parent, name):
    """Compatibility helper for bounded small-DB RAM optimization only."""
    from .streaming import memory_sqlite_snapshot
    return memory_sqlite_snapshot(fd, parent, name)



def normalize_cron(data):
    """Accept Hermes's three storage shapes without silently dropping bad jobs."""
    try:
        payload = json.loads(data)
        if isinstance(payload, list):
            payload = {'jobs': payload}
        if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), (list, dict)):
            raise FinixError('Unsupported cron jobs format')
        jobs = payload['jobs']
        if isinstance(jobs, dict):
            if not all(isinstance(job, dict) for job in jobs.values()):
                raise FinixError('Unsupported cron job')
            jobs = [{**job, 'id': job.get('id') or key} for key, job in jobs.items()]
        if not all(isinstance(job, dict) for job in jobs):
            raise FinixError('Unsupported cron job')
        payload['jobs'] = jobs
        return json.dumps(payload).encode()
    except (ValueError, TypeError, RecursionError):
        raise FinixError('Invalid cron JSON') from None


def make_archive(rootfd):
    inventory = scan(rootfd)
    entries = {}
    companions = set()
    total = 0
    for name, expected in inventory.items():
        if name in companions:
            continue
        with source_file(rootfd, name, expected) as (fd, parent, leaf):
            if os.pread(fd, 16, 0) == b'SQLite format 3\x00':
                data = sqlite_snapshot(fd, parent, leaf)
                companions.update(name + suffix for suffix in ('-wal', '-shm', '-journal'))
            else:
                data = read_regular(fd, MAX_BYTES)
        if name == 'cron/jobs.json':
            if len(data) > 4 * 1024 * 1024:
                raise FinixError('Cron JSON exceeds plaintext limit')
            data = normalize_cron(data)
        total += len(data)
        if total > MAX_BYTES or (name == 'cron/jobs.json' and len(data) > 4 * 1024 * 1024):
            raise FinixError('Snapshot or cron JSON exceeds plaintext limit')
        entries[name] = data
    # SQLite's own read connection may update/create SHM. Companions were
    # type-checked before SQLite opened them; the final scan checks types again.
    # Their bytes/metadata are not the snapshot and must not invalidate its RAM copy.
    final_inventory = scan(rootfd)
    for name in companions & inventory.keys() & final_inventory.keys():
        if inventory[name][:3] != final_inventory[name][:3]:
            raise FinixError('SQLite companion replaced during snapshot')
    if ({n: s for n, s in inventory.items() if n not in companions}
            != {n: s for n, s in final_inventory.items() if n not in companions}):
        raise FinixError('Profile changed during snapshot; retry when quiescent')
    manifest = {'format': 'finix-2', 'files': {
        name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for name, data in entries.items()}}
    encoded_manifest = json.dumps(manifest, sort_keys=True).encode()
    if len(encoded_manifest) > 8 * 1024 * 1024:
        raise FinixError('Manifest exceeds metadata limit')
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz', format=tarfile.USTAR_FORMAT) as archive:
        # Separate metadata from user files, including a user's own manifest.json.
        records = [('manifest.json', encoded_manifest)]
        records.extend(('profile/' + name, data) for name, data in entries.items())
        for name, data in records:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        raise FinixError('Compressed snapshot exceeds limit')
    # Exercise the reader's complete contract before encryption, publication or
    # retention: payload sums omit manifest, tar headers and record padding.
    validate_archive(data)
    return data


def run_age(args, data=None, output=None):
    try:
        result = subprocess.run(['age', *args], input=data, stdout=output or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise FinixError('age could not complete') from None
    if result.returncode:
        raise FinixError('age rejected the key or encrypted data')
    return result.stdout


def keygen(path):
    path = checked_path(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise FinixError('Identity already exists; refusing replacement') from None
    try:
        result = subprocess.run(['age-keygen'], capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise FinixError('Key generation failed')
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(result.stdout)
            stream.flush()
            os.fsync(stream.fileno())
        public = subprocess.run(['age-keygen', '-y', str(path)], capture_output=True,
                                timeout=30, check=False)
        if public.returncode:
            raise FinixError('Key verification failed')
        return public.stdout.decode().strip()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        if fd is not None:
            os.close(fd)


def sha256_file(path, *, cancel=None, progress=None):
    """Hash any regular file in bounded memory, checking for concurrent mutation."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise FinixError('Checksum input must be a regular file')
        from .streaming import check_cancel, report
        digest, count = hashlib.sha256(), 0
        while True:
            check_cancel(cancel)
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
            report(progress, 'checksum', count)
        if (fingerprint(before) != fingerprint(os.fstat(fd))
                or fingerprint(before) != fingerprint(os.stat(path, follow_symlinks=False))):
            raise FinixError('File changed during checksum')
        return digest.hexdigest()
    finally:
        os.close(fd)


def digest_file(path, **kwargs):
    return sha256_file(path, **kwargs)


def sqlite_temp_readiness(path):
    from .streaming import sqlite_temp_readiness as readiness
    return readiness(path)



def atomic_json(path, data):
    fd, temp = tempfile.mkstemp(prefix='.finix-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def load_state(state):
    path = state / 'index.json'
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        data = json.loads(read_regular(fd, 1024 * 1024))
    finally:
        os.close(fd)
    if not isinstance(data, dict) or not re.fullmatch(r'[a-f0-9]{32}', data.get('namespace', '')):
        raise FinixError('Invalid local state')
    if not isinstance(data.get('backups'), list):
        raise FinixError('Invalid local state')
    for record in data['backups']:
        if (not isinstance(record, dict) or not re.fullmatch(
                rf"finix-{data['namespace']}-[a-f0-9]{{32}}\.age", record.get('name', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', record.get('sha256', ''))):
            raise FinixError('Invalid local state')
    return data


def backup(source, output, state, recipient, keep=None, *, sqlite_temp_dir=None,
           cancel=None, progress=None):
    if not RECIPIENT.fullmatch(recipient):
        raise FinixError('Expected one native age public recipient')
    if keep is not None and (type(keep) is not int or keep < 1):
        raise FinixError('Retention must keep at least one backup')
    source, output, state = map(checked_path, (source, output, state))
    if any(a == b or a in b.parents or b in a.parents
           for a, b in [(source, output), (source, state), (output, state)]):
        raise FinixError('Source, output and state must be disjoint directories')
    from .streaming import _temp_directory
    sqlite_temp_dir = _temp_directory(sqlite_temp_dir)
    if any(sqlite_temp_dir == root or root in sqlite_temp_dir.parents
           for root in (source, output, state)):
        raise FinixError('SQLite temporary storage must be outside source, output and state')
    private_dir(output)
    private_dir(state)
    with locked(source) as sourcefd, locked(output), locked(state):
        index = load_state(state)
        binding = hashlib.sha256(str(source).encode()).hexdigest()
        if index is None:
            index = {'namespace': uuid.uuid4().hex, 'source': binding,
                     'output': str(output), 'backups': []}
        if index.get('source') != binding or index.get('output') != str(output):
            raise FinixError('State belongs to a different source or output')
        from .streaming import write_encrypted_snapshot
        name = f"finix-{index['namespace']}-{uuid.uuid4().hex}.age"
        destination = output / name
        fd, temp = tempfile.mkstemp(prefix='.finix-', dir=output)
        try:
            with os.fdopen(fd, 'wb') as stream:
                write_encrypted_snapshot(sourcefd, recipient, stream, sqlite_temp_dir, cancel, progress)
                stream.flush()
                os.fsync(stream.fileno())
            expected = digest_file(temp, cancel=cancel, progress=progress)
            os.link(temp, destination, follow_symlinks=False)
            try:
                if digest_file(destination, cancel=cancel, progress=progress) != expected:
                    raise FinixError('Encrypted output readback verification failed')
            except BaseException:
                destination.unlink()
                raise
        finally:
            Path(temp).unlink(missing_ok=True)
        record = {'name': name, 'sha256': expected,
                  'created_at': datetime.now(timezone.utc).isoformat()}
        index['backups'].append(record)
        # Persist new ownership before any retention deletion. Errors never prune.
        atomic_json(state / 'index.json', index)
        if keep is not None:
            old = index['backups'][:-keep]
            for previous in old:
                path = output / previous['name']
                if path.exists():
                    if digest_file(path) != previous['sha256']:
                        raise FinixError('Retention candidate changed; nothing further pruned')
                    path.unlink()
                index['backups'].remove(previous)
            atomic_json(state / 'index.json', index)
        return {'path': str(destination), 'sha256': expected}


def decrypt(path, identity):
    path, identity = checked_path(path), checked_path(identity)
    # Decryption output is bounded before buffering; never a plaintext disk file.
    cipherfd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    keyfd = None
    process = None
    try:
        original = os.open(identity, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            keydata = read_regular(original, 65536)
        finally:
            os.close(original)
        lines = [line.strip() for line in keydata.decode('ascii').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        if len(lines) != 1 or not re.fullmatch(r'AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}', lines[0]):
            raise FinixError('Only one native age identity is supported; no plugins')
        # Pin validated bytes in RAM so a key-file race cannot introduce an age plugin.
        keyfd = os.memfd_create('finix-identity', os.MFD_CLOEXEC)
        os.write(keyfd, keydata)
        os.lseek(keyfd, 0, os.SEEK_SET)
        if not stat.S_ISREG(os.fstat(cipherfd).st_mode) or os.fstat(cipherfd).st_size > MAX_BYTES + 1048576:
            raise FinixError('Encrypted input exceeds limit or is not regular')
        process = subprocess.Popen(['age', '-d', '-i', f'/proc/self/fd/{keyfd}'],
                                   stdin=cipherfd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   pass_fds=(keyfd,))
        assert process.stdout is not None
        chunks = []
        total = 0
        deadline = time.monotonic() + 30
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise FinixError('Decryption timed out')
                chunk = os.read(process.stdout.fileno(), min(65536, MAX_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    raise FinixError('Decrypted archive exceeds limit')
        data = b''.join(chunks)
        if process.wait(timeout=max(0.01, deadline - time.monotonic())):
            raise FinixError('age rejected the key or encrypted data')
        return data
    finally:
        os.close(cipherfd)
        if keyfd is not None:
            os.close(keyfd)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()


def validate_archive(data):
    # Limit expansion before tar parsing, including metadata and tar padding.
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            raw = compressed.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise FinixError('Archive expansion exceeds limit')
        entries = {}
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            for member in archive:
                candidate = member.name.removeprefix('profile/')
                if (not member.isreg() or member.name in entries or member.pax_headers
                        or not (member.name == 'manifest.json' or safe_name(candidate))
                        or len(entries) >= MAX_FILES + 1 or not 0 <= member.size <= MAX_BYTES):
                    raise FinixError('Unsafe or unsupported archive entry')
                if ((member.name == 'manifest.json' and member.size > 8 * 1024 * 1024)
                        or (candidate == 'cron/jobs.json' and member.size > 4 * 1024 * 1024)):
                    raise FinixError('JSON metadata exceeds limit')
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise FinixError('Missing regular file payload')
                entries[member.name] = extracted.read()
        manifest = json.loads(entries.pop('manifest.json'))
        version = manifest.get('format')
        if version == 'finix-2':
            if any(not n.startswith('profile/') for n in entries):
                raise FinixError('Archive scope mismatch')
            entries = {n[len('profile/'):]: content for n, content in entries.items()}
            admit = allowed
        elif version == 'finix-1':
            admit = legacy_allowed
        else:
            raise FinixError('Unsupported archive format')
        if set(manifest['files']) != set(entries) or any(not admit(n) for n in entries):
            raise FinixError('Archive manifest or scope mismatch')
        for name in entries:
            if any(str(parent) in entries for parent in PurePosixPath(name).parents):
                raise FinixError('Archive file/directory conflict')
        for name, content in entries.items():
            if manifest['files'][name] != {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}:
                raise FinixError('Archive checksum mismatch')
        # Fully validate cron before creating any restored paths.
        if 'cron/jobs.json' in entries:
            jobs = json.loads(entries['cron/jobs.json'])
            if not isinstance(jobs, dict) or not isinstance(jobs.get('jobs'), list):
                raise FinixError('Unsupported cron jobs format')
            for job in jobs['jobs']:
                if not isinstance(job, dict):
                    raise FinixError('Unsupported cron job')
                job.update(enabled=False, state='paused', next_run_at=None, paused_at='finix-restore')
            entries['cron/jobs.json'] = json.dumps(jobs, indent=2).encode()
        return entries
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError, tarfile.TarError, OSError, EOFError):
        raise FinixError('Invalid archive') from None


def restore(archive, identity, target, *, cancel=None, progress=None):
    from .streaming import decrypted_stream, restore_stream
    target = checked_path(target)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FinixError('Restore requires an absent or empty target directory')
    if not target.parent.is_dir():
        raise FinixError('Restore target parent must exist')
    stage = Path(tempfile.mkdtemp(prefix='.finix-restore-', dir=target.parent))
    try:
        with decrypted_stream(archive, identity, cancel) as stream:
            payload, count = restore_stream(stream, stage, cancel, progress)
        # Wait for age authentication, gzip CRC/EOF and full manifest before publish.
        if target.exists():
            with locked(target):
                target.rmdir()
                publish_directory(payload, target)
        else:
            publish_directory(payload, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'target': str(target), 'files': count, 'cron_disabled': True}



def publish_directory(stage, target):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.renameat2(-100, os.fsencode(stage), -100, os.fsencode(target), 1)
    if result:
        raise FinixError('Restore destination changed; candidate not published')


def status(output, state):
    output, state = checked_path(output), checked_path(state)
    index = load_state(state) if state.exists() else None
    if index and index.get('output') != str(output):
        raise FinixError('State belongs to another output')
    records = []
    for record in index['backups'] if index else []:
        path = output / record['name']
        records.append({**record, 'verified': path.exists() and digest_file(path) == record['sha256']})
    return {'backups': records}
