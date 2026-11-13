"""Explicit local-NTFS Windows backend. No emulated POSIX flags.

Every traversed directory is held without FILE_SHARE_DELETE. Opening the
reparse point itself before inspecting its handle prevents junction traversal;
ancestor handles remain alive until the final operation completes.
"""
from contextlib import contextmanager, ExitStack
import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import re

if os.name != 'nt':
    raise ImportError('Windows backend requires Windows')

K = C.WinDLL('kernel32', use_last_error=True)
A = C.WinDLL('advapi32', use_last_error=True)
INVALID = C.c_void_p(-1).value
TOOL_DISCOVERY_VERSION = 'canonical-tools-v1'


def api(dll, name, restype, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, args
    return fn


close = api(K, 'CloseHandle', W.BOOL, W.HANDLE)
create = api(K, 'CreateFileW', W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE)
move = api(K, 'MoveFileExW', W.BOOL, W.LPCWSTR, W.LPCWSTR, W.DWORD)
mutex = api(K, 'CreateMutexW', W.HANDLE, C.c_void_p, W.BOOL, W.LPCWSTR)
wait = api(K, 'WaitForSingleObject', W.DWORD, W.HANDLE, W.DWORD)
release = api(K, 'ReleaseMutex', W.BOOL, W.HANDLE)


class Info(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME), ('accessed', W.FILETIME),
                ('written', W.FILETIME), ('volume', W.DWORD), ('size_hi', W.DWORD),
                ('size_lo', W.DWORD), ('links', W.DWORD), ('index_hi', W.DWORD), ('index_lo', W.DWORD)]


get_info = api(K, 'GetFileInformationByHandle', W.BOOL, W.HANDLE, C.POINTER(Info))


def error(message):
    code = C.get_last_error()
    return OSError(code, message, None, code)


def information(handle):
    value = Info()
    if not get_info(handle, C.byref(value)):
        raise error('Cannot inspect filesystem handle')
    return value


def local_path(path):
    value = os.path.abspath(path)
    if not re.match(r'^[A-Za-z]:\\', value) or value.startswith('\\\\'):
        raise OSError('Only local drive paths are supported')
    get_volume = api(K, 'GetVolumeInformationW', W.BOOL, W.LPCWSTR, W.LPWSTR, W.DWORD,
                     C.c_void_p, C.c_void_p, C.c_void_p, W.LPWSTR, W.DWORD)
    fs = C.create_unicode_buffer(32)
    if not get_volume(value[:3], None, 0, None, None, None, fs, len(fs)) or fs.value != 'NTFS':
        raise OSError('Windows backend requires local NTFS')
    return Path(value)


def canonical_directory(path):
    """Return exact on-disk spelling and identity, never a lowercase identity.

    The direct handle is checked against the no-reparse ancestor walk. Keeping
    both pinned fences substitutions and fails closed on ambiguous NT names.
    Extended DOS prefixes are accepted only for local drive paths.
    """
    value = os.fspath(path)
    if value.startswith('\\\\?\\'):
        value = value[4:]
    value = local_path(value)
    handle = create(str(value), 0x80000000, 3, None, 3, 0x02200000, None)
    if handle == INVALID:
        raise error('Cannot open profile directory')
    try:
        info = information(handle)
        if info.attributes & 0x400 or not info.attributes & 0x10:
            raise OSError('Profile must be a non-reparse directory')
        final_name = api(K, 'GetFinalPathNameByHandleW', W.DWORD,
                         W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD)
        buffer = C.create_unicode_buffer(32768)
        length = final_name(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise error('Cannot resolve profile directory')
        name = buffer.value
        if name.startswith('\\\\?\\'):
            name = name[4:]
        canonical = local_path(name)
        identity = (info.volume, info.index_hi, info.index_lo)
        with directory(canonical) as anchor:
            if anchor.identity() != identity:
                raise OSError('Profile directory identity changed')
            # Existing archive primitives use Win32 case-insensitive lookup.
            # Never silently merge a case-sensitive NTFS subtree through them.
            # Supporting those trees requires a separate exact-name backend.
            query = api(K, 'GetFileInformationByHandleEx', W.BOOL,
                        W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
            for ancestor in anchor.handles:
                flags = W.DWORD()
                if not query(ancestor, 23, C.byref(flags), C.sizeof(flags)):
                    code = C.get_last_error()
                    if code not in (1, 50, 87):
                        raise error('Cannot inspect directory case sensitivity')
                elif flags.value & 1:
                    raise OSError('Case-sensitive Windows profile trees are unsupported')
        return canonical, identity
    finally:
        close(handle)


class Anchor:
    def __init__(self, path, handles):
        self.path, self.handles = path, handles

    @property
    def handle(self):
        return self.handles[-1]

    def identity(self):
        i = information(self.handle)
        return i.volume, i.index_hi, i.index_lo


class UnicodeString(C.Structure):
    _fields_ = [('length', W.WORD), ('maximum', W.WORD), ('buffer', W.LPWSTR)]


class ObjectAttributes(C.Structure):
    _fields_ = [('length', W.ULONG), ('root', W.HANDLE), ('name', C.POINTER(UnicodeString)),
                ('attributes', W.ULONG), ('security', C.c_void_p), ('quality', C.c_void_p)]


class IoStatus(C.Structure):
    _fields_ = [('status', C.c_void_p), ('information', C.c_size_t)]


N = C.WinDLL('ntdll', use_last_error=True)
nt_create = api(N, 'NtCreateFile', W.LONG, C.POINTER(W.HANDLE), W.DWORD,
                C.POINTER(ObjectAttributes), C.POINTER(IoStatus), C.c_void_p,
                W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.c_void_p, W.DWORD)
nt_error = api(N, 'RtlNtStatusToDosError', W.ULONG, W.LONG)


def relative_open(parent_handle, name, *, directory=False, create_new=False, security=None, delete=False, share_write=True):
    """Open exactly one component relative to an existing NT directory handle.

    OBJ_DONT_REPARSE and FILE_OPEN_REPARSE_POINT are both intentional. Ancestor
    reparse mutations cannot redirect resolution through a Win32 pathname.
    """
    if '/' in name or '\\' in name or name in {'', '.', '..'}:
        raise ValueError('Expected one path component')
    admit_name(name)
    if information(parent_handle).attributes & 0x400:
        raise OSError('Directory changed into a reparse point')
    buffer = C.create_unicode_buffer(name)
    encoded_length = len(name.encode('utf-16-le'))
    string = UnicodeString(encoded_length, encoded_length + 2, C.cast(buffer, W.LPWSTR))
    attributes = ObjectAttributes(C.sizeof(ObjectAttributes), parent_handle, C.pointer(string),
                                  0x1040, security, None)
    status, handle = IoStatus(), W.HANDLE()
    options = 0x200000 | 0x20 | (1 if directory else 0x40)
    access = (0xC0000000 if create_new and not directory else 0x80000000) | 0x100000
    if delete:
        access |= 0x10000
    result = nt_create(C.byref(handle), access,
                       C.byref(attributes), C.byref(status), None, 0x80, 3 if share_write else 0,
                       2 if create_new else 1, options, None, 0)
    if result < 0:
        C.set_last_error(nt_error(result))
        raise error('Cannot open anchored filesystem component')
    try:
        info = information(handle)
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            raise OSError('Reparse points and unexpected filesystem types are forbidden')
        # Normalized handle name expands NTFS short names. Never admit a second
        # logical archive prefix through an 8.3 filesystem alias.
        final_name = api(K, 'GetFinalPathNameByHandleW', W.DWORD, W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD)
        buffer = C.create_unicode_buffer(32768)
        length = final_name(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise error('Cannot resolve anchored filesystem name')
        if Path(buffer.value).name.lower() != name.lower():
            raise OSError('Filesystem short-name alias is forbidden')
        return handle.value
    except BaseException:
        close(handle)
        raise


@contextmanager
def directory(path):
    path = local_path(path)
    handles = []
    try:
        for part in [*reversed(path.parents), path]:
            if handles:
                h = relative_open(handles[-1], part.name, directory=True)
            else:
                h = create(str(part), 0x80000000, 3, None, 3, 0x02200000, None)
                if h == INVALID:
                    raise error('Cannot pin volume root')
            handles.append(h)
            info = information(h)
            if info.attributes & 0x400 or not info.attributes & 0x10:
                raise OSError('Reparse points and non-directories are forbidden')
        yield Anchor(path, handles)
    finally:
        for h in reversed(handles):
            close(h)


@contextmanager
def locked(path):
    with directory(path) as anchor:
        name = 'Global\\Finix-' + hashlib.sha256(repr(anchor.identity()).encode()).hexdigest()
        h = mutex(None, False, name)
        if not h:
            raise error('Cannot create directory mutex')
        held = False
        try:
            result = wait(h, 0)
            if result not in (0, 0x80):
                raise OSError('Another Finix operation holds the directory lock')
            held = True
            yield anchor
        finally:
            if held:
                release(h)
            close(h)


def admit_name(name):
    for part in name.split('/'):
        stem = part.split('.')[0].upper()
        if (not part or part in {'.', '..'} or part[-1] in '. '
                or any(c in '<>:"\\|?*' or ord(c) < 32 for c in part)
                or stem in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
                or re.fullmatch(r'(COM|LPT)[1-9¹²³]', stem)):
            raise ValueError('Archive path is not representable safely on Windows')
    return name


def publish_directory(stage, target, *, replace=False):
    """Same-volume handle-relative rename, no replacement unless explicitly requested."""
    source, target = local_path(stage), local_path(target)
    admit_name(target.name)
    rename = api(N, 'NtSetInformationFile', W.LONG, W.HANDLE, C.POINTER(IoStatus),
                 C.c_void_p, W.ULONG, C.c_int)
    class RenameInfo(C.Structure):
        _fields_ = [('replace', W.BYTE), ('root', W.HANDLE), ('length', W.DWORD), ('name', W.WCHAR * 1)]
    encoded = target.name.encode('utf-16-le')
    buffer = C.create_string_buffer(RenameInfo.name.offset + len(encoded))
    info = C.cast(buffer, C.POINTER(RenameInfo)).contents
    info.replace, info.length = bool(replace), len(encoded)
    with directory(source.parent) as parent, directory(target.parent) as destination:
        handle = relative_open(parent.handle, source.name, directory=source.is_dir(), delete=True)
        try:
            info.root = destination.handle
            C.memmove(C.addressof(buffer) + RenameInfo.name.offset, encoded, len(encoded))
            status = IoStatus()
            result = rename(handle, C.byref(status), buffer, len(buffer), 10)
            if result < 0:
                C.set_last_error(nt_error(result))
                raise error('Destination changed; candidate not published')
        finally:
            close(handle)


free = api(K, 'LocalFree', W.HLOCAL, W.HLOCAL)


def sid_text(sid):
    text = W.LPWSTR()
    convert = api(A, 'ConvertSidToStringSidW', W.BOOL, C.c_void_p, C.POINTER(W.LPWSTR))
    if not convert(sid, C.byref(text)):
        raise error('Cannot inspect security principal')
    try:
        return text.value
    finally:
        free(C.cast(text, W.HLOCAL))


def current_sid():
    token = W.HANDLE()
    get_process = api(K, 'GetCurrentProcess', W.HANDLE)
    open_token = api(A, 'OpenProcessToken', W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
    get_token = api(A, 'GetTokenInformation', W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
    if not open_token(get_process(), 8, C.byref(token)):
        raise error('Cannot inspect process token')
    try:
        size = W.DWORD()
        get_token(token, 1, None, 0, C.byref(size))
        buffer = C.create_string_buffer(size.value)
        if not get_token(token, 1, buffer, size, C.byref(size)):
            raise error('Cannot inspect user token')
        return sid_text(C.cast(buffer, C.POINTER(C.c_void_p))[0])
    finally:
        close(token)


class SecurityAttributes(C.Structure):
    _fields_ = [('length', W.DWORD), ('descriptor', C.c_void_p), ('inherit', W.BOOL)]


@contextmanager
def private_security():
    descriptor = C.c_void_p()
    convert = api(A, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', W.BOOL,
                  W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p)
    sddl = 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;' + current_sid() + ')'
    if not convert(sddl, 1, C.byref(descriptor), None):
        raise error('Cannot create private security descriptor')
    try:
        attributes = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
        yield C.byref(attributes)
    finally:
        free(descriptor)


def trusted_handle(handle, *, private=False, ancestor=False):
    owner, dacl, descriptor = C.c_void_p(), C.c_void_p(), C.c_void_p()
    get_security = api(A, 'GetSecurityInfo', W.DWORD, W.HANDLE, C.c_int, W.DWORD,
                       C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p))
    if get_security(handle, 1, 5, C.byref(owner), None, C.byref(dacl), None, C.byref(descriptor)):
        raise OSError('Cannot inspect filesystem security')
    try:
        user = current_sid()
        trusted = {user, 'S-1-5-18', 'S-1-5-32-544',
                   'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'}  # TrustedInstaller
        if sid_text(owner) not in ( {user} if private else trusted ) or not dacl.value:
            return False
        class ACL(C.Structure):
            _fields_ = [('revision', W.BYTE), ('reserved', W.BYTE), ('size', W.WORD), ('count', W.WORD), ('reserved2', W.WORD)]
        acl = C.cast(dacl, C.POINTER(ACL)).contents
        get_ace = api(A, 'GetAce', W.BOOL, C.c_void_p, W.DWORD, C.POINTER(C.c_void_p))
        for index in range(acl.count):
            ace = C.c_void_p()
            if not get_ace(dacl, index, C.byref(ace)):
                raise error('Cannot inspect ACL entry')
            kind, flags = C.string_at(ace, 2)
            if flags & 8:  # INHERIT_ONLY does not apply to this object.
                continue
            if kind == 1:  # Deny entries cannot grant access.
                continue
            if kind != 0:  # Fail closed on object/callback/unknown ACE forms.
                return False
            mask = C.cast(ace.value + 4, C.POINTER(W.DWORD)).contents.value
            principal = sid_text(ace.value + 8)
            # OWNER_RIGHTS applies to this object's owner, already admitted above,
            # not to token group membership or CREATOR_OWNER inheritance.
            # It also replaces implicit owner READ_CONTROL/WRITE_DAC rights;
            # this check classifies grants, it does not promise effective access.
            if principal not in trusted and principal != 'S-1-3-4':
                # FILE_DELETE_CHILD applies to directories, not regular files.
                mutation = 0x10000000 | 0x40000000 | 0x40000 | 0x80000 | 0x10000 | 0x116
                if information(handle).attributes & 0x10:
                    mutation |= 0x40
                    if ancestor:
                        # Creating siblings cannot replace a pinned existing child.
                        # Still forbid delete-child, metadata and ACL mutation.
                        mutation &= ~0x6
                if private or mask & mutation:
                    return False
        return True
    finally:
        free(descriptor)


def trusted_directory(path, *, private=False):
    with directory(path) as anchor:
        return trusted_handle(anchor.handle, private=private)


def private_dir(path):
    path = local_path(path)
    if not path.exists():
        if not path.parent.exists():
            private_dir(path.parent)
        with directory(path.parent) as parent:
            with private_security() as security:
                descriptor = C.cast(security, C.POINTER(SecurityAttributes)).contents.descriptor
                try:
                    handle = relative_open(parent.handle, path.name, directory=True,
                                           create_new=True, security=descriptor)
                except FileExistsError:
                    pass
                else:
                    close(handle)
    if not trusted_directory(path, private=True):
        raise OSError('Private directory has an untrusted owner or ACL')
    return path


@contextmanager
def regular(path, *, create_new=False):
    import msvcrt
    path = local_path(path)
    admit_name(path.name)
    with directory(path.parent) as parent:
        with private_security() as security:
            descriptor = C.cast(security, C.POINTER(SecurityAttributes)).contents.descriptor
            handle = relative_open(parent.handle, path.name, create_new=create_new,
                                   security=descriptor if create_new else None)
        fd = None
        try:
            info = information(handle)
            if info.attributes & (0x400 | 0x10) or info.links != 1:
                raise OSError('Reparse points, hardlinks and non-regular files are forbidden')
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if create_new else os.O_RDONLY))
            handle = None
            yield fd
        finally:
            if fd is not None:
                os.close(fd)
            if handle is not None:
                close(handle)


@contextmanager
def snapshot_regular(path):
    """Retry only native sharing collisions with atomic file producers.

    Reparse/type/ACL failures are never retried or admitted. Keep the ordinary
    opener's non-delete-sharing handle and all its security checks intact.
    """
    import time
    from contextlib import ExitStack
    attempts = 16  # At most 150 ms waiting for transient atomic-rename sharing.
    with ExitStack() as stack:
        for attempt in range(attempts):
            try:
                fd = stack.enter_context(regular(path))
                break
            except OSError as exc:
                if getattr(exc, 'winerror', None) not in (32, 33) or attempt + 1 == attempts:
                    raise
                time.sleep(0.01)
        yield fd


@contextmanager
def source_snapshot_regular(path):
    """Source bytes only: a hardlink is never generic input/execute authority.

    The stat selects an opener, not admission. The exceptional handle is opened
    exclusively relative to pinned no-reparse ancestors, and its owner/DACL
    (shared by every alias) plus ancestor security are checked before reading.
    SQLite must retain its separate single-link, path-based transaction gate.
    Ordinary capture still owns bounded copying and fingerprint stability.
    """
    import msvcrt
    from . import core
    path = local_path(path)
    if path.lstat().st_nlink == 1:
        with snapshot_regular(path) as fd:
            yield fd
        return
    with directory(path.parent) as parent:
        handle = relative_open(parent.handle, path.name, share_write=False)
        fd = None
        try:
            info = information(handle)
            if info.attributes & (0x400 | 0x10) or info.links < 1:
                raise OSError('Source must be a non-reparse regular file')
            if (not trusted_handle(handle) or not trusted_handle(parent.handle)
                    or not all(trusted_handle(h, ancestor=True) for h in parent.handles[:-1])):
                raise OSError('Source hardlink or ancestor has an untrusted owner or ACL')
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDONLY)
            handle = None
            if core.peek(fd, 16) == b'SQLite format 3\x00':
                raise OSError('Hardlinked SQLite sources are forbidden')
            yield fd
        finally:
            if fd is not None:
                os.close(fd)
            if handle is not None:
                close(handle)


def scan(anchor, guard=None):
    from . import core
    result = {}
    def walk(path, prefix=''):
        with directory(path) as pinned:
            if guard is not None:
                guard(pinned, prefix)
            if len(prefix.split('/')) > 32:
                raise core.FinixError('Source directory depth exceeds limit')
            # Windows DirEntry caches the enumeration's type attributes. Retain
            # them so a vanished directory/reparse object cannot masquerade as
            # a transient regular companion merely by its name.
            with os.scandir(path) as entries:
                listed = sorted(entries, key=lambda item: item.name)
            for item in listed:
                entry = Path(item.path)
                relative = prefix + '/' + entry.name if prefix else entry.name
                if core.excluded(relative):
                    continue
                admit_name(relative)
                if not core.allowed(relative):
                    raise core.FinixError('Unsupported source path')
                try:
                    info = entry.lstat()
                    if info.st_file_attributes & 0x400:
                        raise core.FinixError('Included source contains a reparse point')
                    if not info.st_file_attributes & 0x10:
                        with source_snapshot_regular(entry) as fd:
                            result[relative] = core.discovered_source(fd, pinned, entry.name, relative=relative)
                except FileNotFoundError:
                    # Only an already discovered, stable, genuinely readable
                    # SQLite owner may account for a vanished transient sibling.
                    # Never turn a suffix or arbitrary disappearance into exclusion.
                    owner = next((relative[:-len(s)] for s in ('-wal', '-shm', '-journal')
                                  if relative.endswith(s)), None)
                    if (not item.is_file(follow_symlinks=False) or owner not in result
                            or not getattr(result[owner], 'sqlite', False)):
                        raise core.FinixError('Source changed during discovery') from None
                    with source_file(anchor, owner, result[owner]) as (fd, parent, leaf):
                        if core.peek(fd, 16) != b'SQLite format 3\x00':
                            raise core.FinixError('Source changed during discovery') from None
                        connection = connect_source(fd, parent, leaf)
                        connection.close()
                    continue
                # A disappearing subtree is never a transient SQLite file.
                if info.st_file_attributes & 0x10:
                    walk(entry, relative)
    walk(anchor.path)
    return result


@contextmanager
def source_file(anchor, name, expected):
    from . import core
    admit_name(name)
    path = anchor.path.joinpath(*name.split('/'))
    opener = (regular if getattr(expected, 'sqlite', False) else
              snapshot_regular if getattr(expected, 'opaque', None) is not None else
              source_snapshot_regular)
    with directory(path.parent) as parent, opener(path) as fd:
        with core.validated_source(fd, parent, path.name, expected):
            yield fd, parent, path.name


def connect_source(fd, parent, name):
    import sqlite3
    from . import core
    from contextlib import ExitStack
    # Keep companions pinned too: SQLite must never follow a swapped WAL/SHM.
    stack = ExitStack()
    connection = None
    try:
        for suffix in ('-wal', '-shm', '-journal'):
            companion = parent.path / (name + suffix)
            if companion.exists():
                stack.enter_context(regular(companion))
        path = parent.path / name
        if core.fingerprint(os.stat(path))[:2] != core.fingerprint(os.fstat(fd))[:2]:
            raise core.FinixError('SQLite source replaced')
        class PinnedConnection(sqlite3.Connection):
            def close(self):
                try:
                    super().close()
                finally:
                    stack.close()
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2, factory=PinnedConnection)
        connection.execute('BEGIN')
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        stack.close()
        raise


def temp_readiness(path):
    import shutil
    if not trusted_directory(path):
        return {'ready': False, 'code': 'NEED_SQLITE_TEMP_DIRECTORY',
                'message': 'Choose a local NTFS directory without untrusted ACL write access.'}
    return {'ready': True, 'code': 'READY', 'free_bytes': shutil.disk_usage(path).free,
            'message': 'Private ACL-protected temporary SQLite plaintext; crash leftovers may remain.'}


@contextmanager
def sqlite_stage(path, required_bytes):
    import shutil
    import uuid
    from .core import FinixError
    stage = None
    with directory(path):
        if not temp_readiness(path)['ready']:
            raise FinixError('NEED_SQLITE_TEMP_DIRECTORY')
        if shutil.disk_usage(path).free < required_bytes:
            raise FinixError('NOT_ENOUGH_SQLITE_TEMP_SPACE')
        try:
            stage = private_dir(path / ('.finix-sqlite-' + uuid.uuid4().hex))
            yield stage
        finally:
            if stage is not None:
                shutil.rmtree(stage)


def restore_file(stage, name, stream, size, cancel, progress):
    from .streaming import _exact, check_cancel, report, CHUNK
    admit_name(name)
    directory_path = stage
    for component in name.split('/')[:-1]:
        directory_path = private_dir(directory_path / component)
    digest, remaining = hashlib.sha256(), size
    with regular(stage / name, create_new=True) as fd:
        while remaining:
            check_cancel(cancel)
            data = _exact(stream, min(remaining, CHUNK))
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            digest.update(data)
            remaining -= len(data)
            report(progress, 'restore', size - remaining)
        os.fsync(fd)
    return {'size': size, 'sha256': digest.hexdigest()}


def local_appdata():
    """OS-owned canonical LocalAppData, independent of scrubbed/redirected env.

    NO_PACKAGE_REDIRECTION addresses the user's persistent tool installation,
    not an MSIX package's private cache. The caller still pins and ACL-checks
    every path component; this is discovery, not a new trust exception.
    """
    import uuid
    folder = (C.c_ubyte * 16).from_buffer_copy(
        uuid.UUID('f1b32785-6fba-4fcf-9d55-7b8e7f157091').bytes_le)
    shell = C.WinDLL('shell32', use_last_error=True)
    ole = C.WinDLL('ole32', use_last_error=True)
    get_folder = api(shell, 'SHGetKnownFolderPath', W.LONG,
                     C.c_void_p, W.DWORD, W.HANDLE, C.POINTER(C.c_void_p))
    free = api(ole, 'CoTaskMemFree', None, C.c_void_p)
    result = C.c_void_p()
    token = W.HANDLE()
    get_process = api(K, 'GetCurrentProcess', W.HANDLE)
    open_token = api(A, 'OpenProcessToken', W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
    if not open_token(get_process(), 8, C.byref(token)):
        raise OSError('Cannot identify canonical profile')
    try:
        # An explicit process token prevents USERPROFILE expansion through the
        # caller's ambient environment. No token contents are read or returned.
        if get_folder(C.byref(folder), 0x10000, token, C.byref(result)) < 0 or not result.value:
            raise OSError('Cannot resolve canonical LocalAppData')
        return local_path(C.wstring_at(result))
    finally:
        if result.value:
            free(result)
        close(token)


@contextmanager
def trusted_executable(name):
    """Resolve installed Finix tools, then absolute PATH; never CWD search.

    Pin the binary and its ancestors through process creation and validate the
    owner/DACL of each object before permitting execution.
    """
    import msvcrt
    if name not in {'age', 'age-keygen', 'rclone'}:
        raise ValueError('Unsupported executable')
    filename = name + '.exe'
    # Desktop and scheduler do not inherit the optional finix.cmd launcher PATH.
    # Prefer the per-user installation, still subject to the same handle/ACL gate.
    installed = local_appdata() / 'Finix' / 'tools'
    for entry in [str(installed), *os.environ.get('PATH', '').split(os.pathsep)]:
        entry = entry.strip('"')
        if not entry or not Path(entry).is_absolute():
            continue
        parent = local_path(entry)
        # An explicitly OS-discovered installation is not an ambient CWD search.
        if parent == Path.cwd() and parent != installed:
            continue
        candidate = parent / filename
        if not candidate.exists():
            continue
        with directory(parent) as anchor, regular(candidate) as fd:
            if (not trusted_handle(msvcrt.get_osfhandle(fd))
                    or not trusted_handle(anchor.handle)
                    or not all(trusted_handle(handle, ancestor=True) for handle in anchor.handles[:-1])):
                raise PermissionError('Executable or directory has an untrusted owner or ACL')
            yield str(candidate)
            return
    raise FileNotFoundError('Trusted executable not found in Finix tools or absolute PATH')


def find_tool(name):
    """Return a validated absolute name; execution must pin and validate again."""
    with trusted_executable(name) as binary:
        return binary


def run_tool(argv, **kwargs):
    import subprocess
    with trusted_executable(argv[0]) as executable:
        return subprocess.run([executable, *argv[1:]], executable=executable, **kwargs)


def keygen(path):
    import subprocess
    from .core import FinixError
    path = local_path(path)
    created = False
    try:
        with regular(path, create_new=True) as fd:
            created = True
            result = run_tool(['age-keygen'], capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise FinixError('Key generation failed')
            os.write(fd, result.stdout)
            os.fsync(fd)
        public = run_tool(['age-keygen', '-y', str(path)], capture_output=True, timeout=30, check=False)
        if public.returncode:
            raise FinixError('Key verification failed')
        return public.stdout.decode().strip()
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def sha256_file(path, *, cancel=None, progress=None):
    from . import core
    from .streaming import check_cancel, report, CHUNK
    with regular(path) as fd:
        before = core.fingerprint(os.fstat(fd))
        digest, count = hashlib.sha256(), 0
        while True:
            check_cancel(cancel)
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
            report(progress, 'checksum', count)
        if before != core.fingerprint(os.fstat(fd)):
            raise core.FinixError('File changed during checksum')
        return digest.hexdigest()


def restore(archive, identity, target, *, cancel=None, progress=None):
    import shutil
    import uuid
    from . import core, streaming
    target = local_path(target)
    with directory(target.parent):
        if target.exists():
            with directory(target):
                if any(target.iterdir()):
                    raise core.FinixError('Restore requires an absent or empty target directory')
        stage = private_dir(target.parent / ('.finix-restore-' + uuid.uuid4().hex))
        try:
            with streaming.decrypted_stream(archive, identity, cancel) as stream:
                payload, count, layout, completion = streaming.restore_stream(stream, stage, cancel, progress)
            if target.exists():
                # rmdir is atomic and only removes empty directories. A raced-in
                # nonempty destination is preserved; publication also refuses replacement.
                target.rmdir()
            publish_directory(payload, target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {'target': str(target), 'files': count, 'cron_disabled': True, 'layout': layout, **completion}


class Pipe:
    """Blocking Windows anonymous pipes serviced off-thread, one chunk at a time.

    No selector/socket assumption and no unbounded queues. Cancellation unwinds
    the owning process context, which kills the child and releases pending I/O.
    """
    def __init__(self, stream, cancel):
        self.stream, self.cancel = stream, cancel

    def _io(self, action):
        import threading
        from .streaming import check_cancel
        done, result = threading.Event(), []
        def run():
            try:
                result.append((True, action()))
            except BaseException as exc:
                result.append((False, exc))
            finally:
                done.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        while not done.wait(.1):
            check_cancel(self.cancel)
        check_cancel(self.cancel)
        thread.join()
        ok, value = result[0]
        if not ok:
            raise value
        return value

    def read(self, size=-1):
        from .streaming import CHUNK
        if size < 0:
            raise ValueError('Unbounded pipe read rejected')
        if size == 0:
            return b''
        return self._io(lambda: os.read(self.stream.fileno(), min(size, CHUNK)))

    def write(self, data):
        from .streaming import CHUNK
        view = memoryview(data)
        count = len(view)
        while view:
            chunk = view[:CHUNK]
            written = self._io(lambda: os.write(self.stream.fileno(), chunk))
            view = view[written:]
        return count

    def flush(self):
        pass


@contextmanager
def decrypted_stream(path, identity, cancel=None):
    import subprocess
    from . import core, streaming
    with regular(path) as cipherfd, regular(identity) as keyfd:
        data = core.read_regular(keyfd, 65536)
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise core.FinixError('INVALID_IDENTITY') from None
        lines = [line.strip() for line in text.splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        if len(lines) != 1 or not re.fullmatch(r'AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}', lines[0]):
            raise core.FinixError('Only one native age identity is supported; no plugins')
        # age reads validated identity bytes from stdin. Ciphertext is a pinned
        # filename, never a key path or key bytes in argv, env or a plaintext temp.
        process = popen(['age', '-d', '-i', '-', str(local_path(path))],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            Pipe(process.stdin, cancel).write((lines[0] + '\n').encode('ascii'))
            process.stdin.close()
            yield Pipe(process.stdout, cancel)
            streaming._wait(process, cancel)
        finally:
            streaming._cleanup(process)


class BasicLimits(C.Structure):
    _fields_ = [('process_time', C.c_longlong), ('job_time', C.c_longlong), ('flags', W.DWORD),
                ('min_working', C.c_size_t), ('max_working', C.c_size_t), ('active', W.DWORD),
                ('affinity', C.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]


class IoCounters(C.Structure):
    _fields_ = [(n, C.c_ulonglong) for n in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class ExtendedLimits(C.Structure):
    _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('process_memory', C.c_size_t),
                ('job_memory', C.c_size_t), ('peak_process', C.c_size_t), ('peak_job', C.c_size_t)]


class ThreadEntry(C.Structure):
    _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('thread', W.DWORD),
                ('process', W.DWORD), ('priority', W.LONG), ('delta', W.LONG), ('flags', W.DWORD)]


def popen(argv, **kwargs):
    if argv[0] in {'age', 'age-keygen'}:
        with trusted_executable(argv[0]) as executable:
            return _popen([executable, *argv[1:]], executable=executable, **kwargs)
    return _popen(argv, **kwargs)


def _popen(argv, **kwargs):
    """Create suspended, assign kill-on-close Job Object, then resume primary thread.

    No child user code can execute before job membership. Non-inherited job handle
    guarantees cleanup on parent death as well as normal cancellation.
    """
    import subprocess
    make_job = api(K, 'CreateJobObjectW', W.HANDLE, C.c_void_p, W.LPCWSTR)
    set_job = api(K, 'SetInformationJobObject', W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
    assign = api(K, 'AssignProcessToJobObject', W.BOOL, W.HANDLE, W.HANDLE)
    snapshot = api(K, 'CreateToolhelp32Snapshot', W.HANDLE, W.DWORD, W.DWORD)
    first = api(K, 'Thread32First', W.BOOL, W.HANDLE, C.POINTER(ThreadEntry))
    next_thread = api(K, 'Thread32Next', W.BOOL, W.HANDLE, C.POINTER(ThreadEntry))
    open_thread = api(K, 'OpenThread', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    resume = api(K, 'ResumeThread', W.DWORD, W.HANDLE)
    job = make_job(None, None)
    if not job:
        raise error('Cannot create child job')
    process = None
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000
        if not set_job(job, 9, C.byref(limits), C.sizeof(limits)):
            raise error('Cannot configure child job')
        kwargs.pop('start_new_session', None)
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | 4 | 0x08000000
        process = subprocess.Popen(argv, **kwargs)
        if not assign(job, W.HANDLE(int(process._handle))):
            raise error('Cannot contain child process')
        threads = snapshot(4, 0)
        if threads == INVALID:
            raise error('Cannot inspect suspended child')
        resumed = False
        try:
            entry = ThreadEntry()
            entry.size = C.sizeof(entry)
            found = first(threads, C.byref(entry))
            while found:
                if entry.process == process.pid:
                    thread = open_thread(2, False, entry.thread)
                    if not thread:
                        raise error('Cannot open suspended child thread')
                    try:
                        if resume(thread) == 0xFFFFFFFF:
                            raise error('Cannot resume contained child')
                        resumed = True
                    finally:
                        close(thread)
                    break
                found = next_thread(threads, C.byref(entry))
        finally:
            close(threads)
        if not resumed:
            raise OSError('Suspended child thread missing')
        process._finix_job = job
        job = None
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise
    finally:
        if job is not None:
            close(job)


def cleanup(process):
    job = getattr(process, '_finix_job', None)
    if job is not None:
        process._finix_job = None
        close(job)
    elif process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def output_events(process, deadline):
    """Drain both anonymous pipes with bounded backpressure and a wall deadline."""
    import queue
    import threading
    import time
    events, stop = queue.Queue(maxsize=8), threading.Event()
    def publish(item):
        while not stop.is_set():
            try:
                events.put(item, timeout=.1)
                return
            except queue.Full:
                pass
    def drain(stream, stdout):
        try:
            while not stop.is_set():
                data = os.read(stream.fileno(), 4096)
                if not data:
                    break
                publish((stdout, data))
        except OSError:
            pass
        finally:
            publish((stdout, None))
    threads = [threading.Thread(target=drain, args=(s, out), daemon=True)
               for s, out in ((process.stdout, True), (process.stderr, False))]
    for thread in threads:
        thread.start()
    remaining = 2
    try:
        while remaining:
            if time.monotonic() >= deadline:
                raise TimeoutError('Child output deadline exceeded')
            try:
                stdout, data = events.get(timeout=.1)
            except queue.Empty:
                continue
            if data is None:
                remaining -= 1
            else:
                yield stdout, data
    finally:
        stop.set()


def trusted_binary(path):
    import msvcrt
    path = local_path(path)
    if path.suffix.lower() != '.exe':
        return False
    with directory(path.parent) as anchor, regular(path) as fd:
        return (trusted_handle(msvcrt.get_osfhandle(fd)) and trusted_handle(anchor.handle)
                and all(trusted_handle(handle, ancestor=True) for handle in anchor.handles[:-1]))


def guard_tree(anchor, relative, *, root_name):
    from .core import FinixError
    names = {name.lower() for name in os.listdir(anchor.path)}
    if 'profiles' not in names:
        return
    leaf = (Path(relative).name if relative else root_name).lower()
    recognized = leaf == '.hermes' or bool(names & {'config.yaml', '.env', 'state.db'})
    if not recognized:
        profiles = anchor.path / 'profiles'
        if profiles.is_dir():
            with directory(profiles):
                marker = profiles / '.deleted'
                if marker.exists():
                    with directory(marker):
                        recognized = True
    if recognized:
        raise FinixError('Extra source contains another Hermes profile tree')
