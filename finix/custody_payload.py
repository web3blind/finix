"""Versioned opaque byte containers are ordinary DATA, never SQLite authority."""
import struct
import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from . import core

_LIMIT=16*1024*1024
STORAGE_FORMAT = 'opaque-container-v2'
MAGIC = b'FINIXCUSTODY\x00\x02\r\n'
_HEADER = struct.Struct('>16sQ32s')
MAX_PAYLOAD = 1024 ** 4  # Finite byte budget, not a file-count cap.


def _header(size, sha256):
    if type(size) is not int or not 0 <= size <= MAX_PAYLOAD or not _hex(sha256):
        raise ValueError('Invalid logical payload')
    return _HEADER.pack(MAGIC, size, bytes.fromhex(sha256))


def encode(fd, logical, output=None):
    """Stream raw bytes once; return stored size/hash, never open SQLite."""
    before = core.fingerprint(os.fstat(fd))
    header = _header(logical['size'], logical['sha256'])
    raw, stored = hashlib.sha256(), hashlib.sha256(header)
    if output is not None: output.write(header)
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = logical['size']
    while remaining:
        chunk = os.read(fd, min(1024*1024, remaining))
        if not chunk: raise ValueError('Truncated raw payload')
        remaining -= len(chunk); raw.update(chunk); stored.update(chunk)
        if output is not None: output.write(chunk)
    if os.read(fd, 1) or raw.hexdigest() != logical['sha256'] or before != core.fingerprint(os.fstat(fd)):
        raise ValueError('Changed raw payload')
    return dict(size=len(header)+logical['size'], sha256=stored.hexdigest())


def decode(fd, output=None):
    """Strict bounded envelope validation; optional byte-exact raw readback."""
    before = core.fingerprint(os.fstat(fd))
    os.lseek(fd, 0, os.SEEK_SET)
    header = os.read(fd, _HEADER.size)
    if len(header) != _HEADER.size: raise ValueError('Truncated container')
    magic, size, digest = _HEADER.unpack(header)
    if magic != MAGIC or size > MAX_PAYLOAD or before[3] != _HEADER.size + size:
        raise ValueError('Invalid container')
    raw, stored = hashlib.sha256(), hashlib.sha256(header)
    remaining = size
    while remaining:
        chunk = os.read(fd, min(1024*1024, remaining))
        if not chunk: raise ValueError('Truncated container')
        remaining -= len(chunk); raw.update(chunk); stored.update(chunk)
        if output is not None: output.write(chunk)
    if os.read(fd, 1) or raw.digest() != digest or before != core.fingerprint(os.fstat(fd)):
        raise ValueError('Invalid container digest')
    return dict(size=size, sha256=raw.hexdigest()), dict(size=_HEADER.size+size, sha256=stored.hexdigest())



def _hex(value, length=64):
    return type(value) is str and re.fullmatch('[a-f0-9]{' + str(length) + '}', value) is not None


def _identity(value):
    return (type(value) is list and len(value) == 2
            and all(type(x) is int and 0 <= x < 2 ** 128 for x in value) and value[1] > 0)


def _leaf(value):
    return (type(value) is dict and set(value) == {'identity', 'size', 'sha256'}
            and _identity(value['identity']) and type(value['size']) is int
            and value['size'] >= 0 and _hex(value['sha256']))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_manifest(m):
    """Strict v2 DATA schema; historical native identities are provenance only."""
    from pathlib import PureWindowsPath, PurePosixPath
    family = {'state.db', 'state.db-wal', 'state.db-shm', 'state.db-journal'}
    if (type(m) is not dict
            or set(m) != {'version', 'policy', 'storage_format', 'namespace', 'stored', 'plan_digest', 'root_identity', 'parents', 'old', 'current'}
            or type(m['version']) is not int or m['version'] != 2
            or m['storage_format'] != STORAGE_FORMAT or not _hex(m['namespace'], 32)
            or type(m['stored']) is not dict
            or m['policy'] != 'runtime-attempt-preserve-v1'
            or not _hex(m['plan_digest']) or not _identity(m['root_identity'])):
        raise ValueError()
    old, current, parents = m['old'], m['current'], m['parents']
    if (type(old) is not dict or type(current) is not dict or not current
            or set(old) != set(current) or type(parents) is not dict or not parents):
        raise ValueError()
    folded = set()
    for name in current:
        if (type(name) is not str or not core.safe_name(name) or not core.allowed(name)
                or core.excluded(name) or name.split('/')[0] == 'recovery-custody'
                or name.casefold() in folded
                or ((name.casefold() == 'state.db' or name.casefold().startswith('state.db-')) and name not in family)):
            raise ValueError()
        folded.add(name.casefold())
        for leaf in (old[name], current[name]):
            if leaf is not None and not _leaf(leaf):
                raise ValueError()
    if family.intersection(current) and not family <= set(current):
        raise ValueError()
    roots = [n for n, identity in parents.items() if identity == m['root_identity']]
    if len(roots) != 1 or any(not _identity(i) for i in parents.values()) or type(roots[0]) is not str:
        raise ValueError()
    # These are historical paths/IDs. Never reopen them or compare to a new home.
    path_type = PureWindowsPath if PureWindowsPath(roots[0]).is_absolute() else PurePosixPath
    root = path_type(roots[0])
    if not root.is_absolute() or str(root) != roots[0] or '..' in root.parts:
        raise ValueError()
    if set(parents) != {str((root / path_type(n)).parent) for n in current}:
        raise ValueError()
    expected = {kind + '/' + hashlib.sha256(n.encode('utf8')).hexdigest(): leaf
                for kind, leaves in [('old', old), ('copies', current), ('objects', current)]
                for n, leaf in leaves.items() if leaf is not None}
    if set(m['stored']) != set(expected): raise ValueError()
    for name, leaf in expected.items():
        stored = m['stored'][name]
        if (type(stored) is not dict or set(stored) != {'size', 'sha256'}
                or type(stored['size']) is not int or stored['size'] != _HEADER.size + leaf['size']
                or leaf['size'] > MAX_PAYLOAD or not _hex(stored['sha256'])): raise ValueError()
    return m


def _private(fd, directory=False):
    info = os.fstat(fd)
    if directory:
        if not stat.S_ISDIR(info.st_mode): raise ValueError()
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ValueError()
    if os.name != 'nt':
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077: raise ValueError()
    else:
        import msvcrt
        from .windows_native import trusted_handle
        if not trusted_handle(msvcrt.get_osfhandle(fd), private=True): raise ValueError()


@contextmanager
def _directory(parent, name):
    if os.name == 'nt':
        from .windows_native import directory, trusted_handle
        with directory(parent.path / name) as opened:
            if not trusted_handle(opened.handle, private=True): raise ValueError()
            yield opened
    else:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            _private(fd, directory=True)
            yield fd
        finally: os.close(fd)


def _list(parent):
    return set(os.listdir(parent.path if os.name == 'nt' else parent))


def _record(parent, name):
    with ExitStack() as stack:
        if os.name == 'nt': fd = stack.enter_context(core.regular_input(parent.path / name))
        else:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stack.callback(os.close, fd)
        _private(fd)
        data = core.read_regular(fd, _LIMIT)
    return json.loads(data, object_pairs_hook=_pairs), dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())


def _namespace(base, namespace):
    m, manifest_bytes = _record(base, 'execution-manifest.json')
    validate_manifest(m)
    if m['namespace'] != namespace: raise ValueError()
    p, progress_bytes = _record(base, 'progress.json')
    if (type(p) is not dict or set(p) != {'version', 'manifest_digest', 'state', 'pending', 'restore'}
            or type(p['version']) is not int or p['version'] != 2
            or p['manifest_digest'] != _digest(m) or p['state'] != 'restored'
            or p['pending'] is not None or type(p['restore']) is not dict): raise ValueError()
    restored = {n: leaf for n, leaf in m['old'].items() if leaf is not None}
    if set(p['restore']) != set(restored): raise ValueError()
    for name, old in restored.items():
        leaf = p['restore'][name]
        if not _leaf(leaf) or (leaf['sha256'], leaf['size']) != (old['sha256'], old['size']): raise ValueError()
    bindings = {'execution-manifest.json': manifest_bytes, 'progress.json': progress_bytes}
    kinds = {'old', 'copies', 'objects', 'restore-staging'}
    listed = _list(base)
    if not set(bindings) <= listed or listed - set(bindings) - kinds: raise ValueError()
    for kind in kinds:
        leaves = {} if kind == 'restore-staging' else m['old' if kind == 'old' else 'current']
        expected = {hashlib.sha256(n.encode('utf8')).hexdigest(): leaf for n, leaf in leaves.items() if leaf is not None}
        if kind not in listed:
            if expected: raise ValueError()
            continue  # Empty directories are not represented in archives.
        with _directory(base, kind) as folder:
            if _list(folder) != set(expected): raise ValueError()
        bindings.update({kind + '/' + token: dict(m['stored'][kind + '/' + token],
                        logical=dict(size=leaf['size'], sha256=leaf['sha256'])) for token, leaf in expected.items()})
    generation = dict(namespace=namespace, manifest_sha256=manifest_bytes['sha256'],
                      progress_sha256=progress_bytes['sha256'], plan_digest=m['plan_digest'],
                      historical_root=m['root_identity'])
    return {name: dict(binding, generation=generation) for name, binding in bindings.items()}


def inventory(root, discovered):
    """Require all members, including those absent before discovery."""
    try:
        if 'recovery-custody' not in _list(root):
            if any(n.startswith('recovery-custody/') for n in discovered): raise ValueError()
            return
        with _directory(root, 'recovery-custody') as custody:
            expected_names = set()
            for namespace in sorted(_list(custody)):
                if not _hex(namespace, 32): raise ValueError()
                with _directory(custody, namespace) as base: bindings = _namespace(base, namespace)
                prefix = 'recovery-custody/' + namespace + '/'
                actual = {n[len(prefix):]: fp for n, fp in discovered.items() if n.startswith(prefix)}
                if set(actual) != set(bindings): raise ValueError()
                for name, binding in bindings.items():
                    if getattr(actual[name], 'opaque', None) != binding: raise ValueError()
                expected_names.update(prefix + name for name in bindings)
            if {n for n in discovered if n.startswith('recovery-custody/')} != expected_names: raise ValueError()
    except core.FinixError: raise
    except (ValueError, KeyError, TypeError, OSError, RecursionError):
        raise core.FinixError('CUSTODY_MANIFEST_INVALID') from None

def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise core.FinixError('CUSTODY_MANIFEST_INVALID')
        result[key]=value
    return result

def classify(fd, parent, relative):
    """Bind raw bytes to the whole validated generation, including metadata."""
    match = re.fullmatch(r'recovery-custody/([a-f0-9]{32})/(.+)', relative or '')
    if match is None: return None
    try:
        parts = match[2].split('/')
        if not (len(parts) == 1 and parts[0] in ('execution-manifest.json', 'progress.json')
                or len(parts) == 2 and parts[0] in ('old', 'copies', 'objects', 'restore-staging') and _hex(parts[1])):
            raise ValueError()
        with ExitStack() as stack:
            base = parent if len(parts) == 1 else stack.enter_context(_directory(parent, '..'))
            stack.enter_context(_directory(base, '..'))
            binding = _namespace(base, match[1])[match[2]]
        _private(fd)
        if 'logical' in binding:
            logical, stored = decode(fd)
            if logical != binding['logical'] or stored != {k: binding[k] for k in ('size', 'sha256')}: raise ValueError()
        before = core.fingerprint(os.fstat(fd))
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        digest, size = hashlib.sha256(), 0
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        finally: os.lseek(fd, pos, os.SEEK_SET)
        if (before != core.fingerprint(os.fstat(fd)) or size != binding['size']
                or digest.hexdigest() != binding['sha256']): raise ValueError()
        return binding
    except core.FinixError: raise
    except (ValueError, KeyError, TypeError, OSError, RecursionError):
        raise core.FinixError('CUSTODY_MANIFEST_INVALID') from None
