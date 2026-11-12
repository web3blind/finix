"""Google Workspace storage using the owning profile's credential-owning helper.

No credential files are opened by Finix. Local readiness never contacts Google.
The private ledger, not remote names/listings, is the authority for retention.
"""

from contextlib import contextmanager, ExitStack
import stat
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import threading
import hashlib

JOURNAL_BYTES = 16 * 1024 * 1024

from . import core
from . import cloud_files
from .streaming import check_cancel


MAX_RECORDS = 1000
MIME = 'application/octet-stream'
FOLDER_MIME = 'application/vnd.google-apps.folder'


class WorkspaceError(core.FinixError):
    def __init__(self, code):
        self.code = code
        super().__init__('Workspace ' + code + ': use hermes auth add finix:google in this profile; legacy Workspace data requires explicit migration; no fallback attempted')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise core.FinixError('Invalid Workspace object ID')
    return value


def child_env(home):
    # Never import/overlay the parent's secrets or dotenv. HOME is only used for
    # normal OS libraries; OAuth resolution is explicitly HERMES_HOME-bound.
    base = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
    if os.name == 'nt':
        for key in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'):
            if key in os.environ:
                base[key] = os.environ[key]
    base['HERMES_HOME'] = str(home)
    base['PYTHONNOUSERSITE'] = '1'
    return base


def _pin_linux_invocation(invocation, target, stack):
    """Hold every invocation component, including venv symlink hops and targets."""
    pending = list(invocation.parts[1:])
    current, hops = Path('/'), 0
    while pending:
        component = pending.pop(0)
        if component in ('', '.'):
            continue
        if component == '..':
            current = current.parent
            continue
        path = current / component
        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        link = stat.S_ISLNK(info.st_mode)
        sticky = stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (info.st_uid not in {0, os.getuid()}
                or (not link and info.st_mode & 0o022 and not sticky)):
            raise WorkspaceError('UNTRUSTED_HELPER')
        if link:
            hops += 1
            if hops > 40:
                raise WorkspaceError('UNTRUSTED_HELPER')
            # Read the pinned symlink itself, not a potentially replaced name.
            destination = Path(os.readlink('', dir_fd=fd))
            if destination.is_absolute():
                current = Path('/')
                pending = list(destination.parts[1:]) + pending
            else:
                pending = list(destination.parts) + pending
        else:
            if not (stat.S_ISDIR(info.st_mode) if pending else stat.S_ISREG(info.st_mode)):
                raise WorkspaceError('UNTRUSTED_HELPER')
            current = path
    if current != target or invocation.resolve(strict=True) != target:
        raise WorkspaceError('UNTRUSTED_HELPER')


def _python_runtime():
    """Use only this host SDK's PM contract, never ambient import-path forwarding.

    A standalone wheel/CLI without Hermes keeps its own interpreter. An SDK host
    must still run the selected generation: a mid-process PM change fails closed.
    """
    try:
        import hermes_constants
    except ModuleNotFoundError as exc:
        if exc.name == 'hermes_constants':
            return [sys.executable, '-I', '-B'], None, None
        raise WorkspaceError('UNTRUSTED_HELPER') from None
    try:
        from pm import environments
        root = Path(hermes_constants.__file__).resolve(strict=True).parent
        if Path(environments.__file__).resolve(strict=True) != root / 'pm/environments.py':
            raise WorkspaceError('UNTRUSTED_HELPER')
        environment = environments.selected_venv(root)
        if not environments.running_from_selected_environment(root):
            raise WorkspaceError('UNTRUSTED_HELPER')
        command = environments.venv_command(root, environment, ['-I', '-B'])
        if not command or not Path(command[0]).is_absolute():
            raise WorkspaceError('UNTRUSTED_HELPER')
        return command, environment, environments
    except (ImportError, AttributeError, OSError, RuntimeError, ValueError):
        raise WorkspaceError('UNTRUSTED_HELPER') from None


@contextmanager
def trusted_runtime(script, *, entry=None):
    """Pin the explicit interpreter, wrapper and owning provider; never search PATH."""
    if not sys.executable or not Path(sys.executable).is_absolute():
        raise WorkspaceError('UNTRUSTED_HELPER')
    try:
        prefix, environment, selection = _python_runtime()
        invocation = Path(prefix[0])
        binary = invocation.resolve(strict=True)
        wrapper = Path(entry) if entry else Path(__file__).absolute().with_name('workspace_helper.py')
        paths = list(dict.fromkeys([binary, *([invocation] if os.name == 'nt' else []), wrapper, Path(script),
                                   *([environment / 'pyvenv.cfg'] if environment else []),
                                   *[Path(p) for p in prefix[1:] if not p.startswith('-') and Path(p).is_file()]]))
        with ExitStack() as stack:
            if environment:
                # Keep the selected tree and site-packages anchors alive through spawn.
                for directory in (environment, selection.site_packages(environment)):
                    if os.name == 'nt':
                        import msvcrt
                        from . import windows_native as win
                        anchor = stack.enter_context(win.directory(directory))
                        if (not win.trusted_handle(anchor.handle)
                                or not all(win.trusted_handle(h, ancestor=True) for h in anchor.handles[:-1])):
                            raise WorkspaceError('UNTRUSTED_HELPER')
                    else:
                        for parent in [*reversed(directory.parents), directory]:
                            fd = os.open(parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
                            stack.callback(os.close, fd)
                            info = os.fstat(fd)
                            sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                            if info.st_uid not in {0, os.getuid()} or (info.st_mode & 0o022 and not sticky):
                                raise WorkspaceError('UNTRUSTED_HELPER')
            if os.name == 'nt':
                import msvcrt
                from . import windows_native as win
                if not all(callable(getattr(win, name, None)) for name in ('popen', 'output_events', 'cleanup')):
                    raise WorkspaceError('PLATFORM_UNAVAILABLE')
                if binary.suffix.lower() != '.exe':
                    raise WorkspaceError('UNTRUSTED_HELPER')
                for path in paths:
                    if not path.is_absolute():
                        raise WorkspaceError('UNTRUSTED_HELPER')
                    anchor = stack.enter_context(win.directory(path.parent))
                    fd = stack.enter_context(win.regular(path))
                    if (not win.trusted_handle(msvcrt.get_osfhandle(fd))
                            or not win.trusted_handle(anchor.handle)
                            or not all(win.trusted_handle(h, ancestor=True) for h in anchor.handles[:-1])):
                        raise WorkspaceError('UNTRUSTED_HELPER')
            elif sys.platform == 'linux':
                import resource
                if (not callable(getattr(resource, 'setrlimit', None))
                        or not all(hasattr(resource, x) for x in ('RLIMIT_FSIZE', 'RLIMIT_CORE'))):
                    raise WorkspaceError('PLATFORM_UNAVAILABLE')
                _pin_linux_invocation(invocation, binary, stack)
                for path in paths:
                    if not path.is_absolute():
                        raise WorkspaceError('UNTRUSTED_HELPER')
                    for parent in [*reversed(path.parents), path]:
                        fd = os.open(parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        stack.callback(os.close, fd)
                        info = os.fstat(fd)
                        sticky = stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                        if (info.st_uid not in {0, os.getuid()}
                                or (info.st_mode & 0o022 and not sticky)
                                or not (stat.S_ISDIR(info.st_mode) if parent != path else stat.S_ISREG(info.st_mode))):
                            raise WorkspaceError('UNTRUSTED_HELPER')
            else:
                raise WorkspaceError('PLATFORM_UNAVAILABLE')
            if environment and selection.selected_venv(Path(selection.__file__).resolve().parent.parent) != environment:
                raise WorkspaceError('UNTRUSTED_HELPER')
            yield prefix, str(wrapper)
    except (OSError, ImportError, RuntimeError, ValueError):
        raise WorkspaceError('UNTRUSTED_HELPER') from None


class WorkspaceRunner:
    """Legacy journal protocol using only Finix's declared Google OAuth transport."""
    def __init__(self, home, *, request=None, **unused):
        self.home = core.checked_path(home)
        from .drive import Drive
        self.drive = Drive('0' * 64, request=request or self._request)

    def _request(self, method, url, **kwargs):
        from .drive import managed_request, profile_scope
        with profile_scope(self.home):
            return managed_request(method, url, **kwargs)

    def __call__(self, operation, *args, expected_size=None, cancel=None, **unused):
        from .drive import BASE, UPLOAD, MEDIA_CHUNK, upload_media
        check_cancel(cancel)
        def option(name):
            return args[args.index(name) + 1]
        def call(method, url, payload=None, headers=None, limit=1024*1024):
            check_cancel(cancel)
            response = self.drive.response(method, url, payload, headers, limit=limit, missing_ok=True)
            check_cancel(cancel)
            if response is None:
                raise WorkspaceError('NOT_FOUND')
            return response
        if operation == 'identity':
            return {'account_id': call('GET', BASE.replace('/files', '/about') + '?fields=user(permissionId)').json()['user']['permissionId']}
        if operation == 'generate-id':
            return {'id': identifier(call('GET', BASE + '/generateIds?count=1&space=drive&type=files').json()['ids'][0])}
        if operation == 'metadata':
            return call('GET', BASE + '/' + identifier(args[0]) + '?fields=id,name,mimeType,size,parents,owners(emailAddress),trashed').json()
        if operation == 'create-folder':
            payload = {'id': identifier(option('--id')), 'name': option('--name'), 'mimeType': FOLDER_MIME}
            return dict(call('POST', BASE + '?fields=id', payload).json(), status='created')
        if operation == 'upload':
            path = core.checked_path(args[0])
            size = path.stat().st_size
            payload = {'id': identifier(option('--id')), 'name': option('--name'),
                       'parents': [identifier(option('--parent'))], 'mimeType': MIME}
            response = call('POST', UPLOAD, payload, {'X-Upload-Content-Type': MIME, 'X-Upload-Content-Length': str(size)})
            uri = next((v for k,v in response.headers.items() if k.lower() == 'location'), None)
            return dict(upload_media(uri, path, size, cancel=cancel), status='uploaded')
        if operation == 'download':
            if type(expected_size) is not int or expected_size <= 0:
                raise WorkspaceError('INVALID_STATE')
            path = core.checked_path(option('--output'))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                for start in range(0, expected_size, MEDIA_CHUNK):
                    end = min(start + MEDIA_CHUNK, expected_size) - 1
                    response = call('GET', BASE + '/' + identifier(args[0]) + '?alt=media',
                        headers={'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'}, limit=end-start+1)
                    headers = {k.lower():v for k,v in response.headers.items()}
                    if (response.status != 206 or headers.get('content-range') != f'bytes {start}-{end}/{expected_size}'
                            or headers.get('content-encoding', 'identity') != 'identity' or len(response.body) != end-start+1):
                        raise WorkspaceError('INVALID_RESPONSE')
                    stream.write(response.body)
            return {'status': 'downloaded', 'id': args[0]}
        if operation == 'delete':
            call('PATCH', BASE + '/' + identifier(args[0]), {'trashed': True})
            return {'status': 'trashed', 'fileId': args[0], 'permanent': False}
        raise WorkspaceError('UNSUPPORTED_OPERATION')


class WorkspaceDrive:
    def __init__(self, home, profile_id, state, *, runner=None, account_digest=None):
        if not isinstance(profile_id, str) or not re.fullmatch(r'[a-f0-9]{64}', profile_id):
            raise core.FinixError('Invalid Finix profile binding')
        self.home, self.owner, self.state = core.checked_path(home), profile_id, core.checked_path(state)
        self.runner = runner or WorkspaceRunner(self.home)
        self.account_digest = account_digest
        self._operation_lock = threading.RLock()
        self._selection = (str(self.home), self.owner, str(self.state), self.account_digest)
        self._thread_state = threading.local()
        self._home_identity = self._directory_identity(self.home)
        self._state_identity = self._directory_identity(self.state) if self.state.exists() else None

    @staticmethod
    def _directory_identity(path):
        if os.name == 'nt':
            from . import windows_native as win
            with win.directory(path) as anchor:
                return list(anchor.identity())
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            return [info.st_dev, info.st_ino]
        finally:
            os.close(fd)

    def _binding(self):
        if (str(self.home), self.owner, str(self.state), self.account_digest) != self._selection:
            raise WorkspaceError('BINDING_CHANGED')
        home = self._directory_identity(self.home)
        state = self._directory_identity(self.state)
        if home != self._home_identity or (self._state_identity is not None and state != self._state_identity):
            raise WorkspaceError('BINDING_CHANGED')
        if isinstance(self.runner, WorkspaceRunner) and self.runner.home != self.home:
            raise WorkspaceError('BINDING_CHANGED')
        self._state_identity = state
        return dict(provider='finix:google', profile_id=self.owner, home=str(self.home),
                    home_identity=home, state=str(self.state), state_identity=state)

    @contextmanager
    def _locked(self):
        with self._operation_lock:
            if getattr(self._thread_state, 'active', False):
                yield
                return
            core.private_dir(self.state)
            from .backup_jobs import file_lease
            with core.locked(self.state) as anchor, file_lease(self.state / 'workspace-operation.lock'):
                if os.name == 'nt':
                    from . import windows_native as win
                    safe = win.trusted_handle(anchor.handle, private=True)
                else:
                    info = os.fstat(anchor)
                    safe = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
                if not safe:
                    raise WorkspaceError('STATE_PERMISSIONS')
                self._thread_state.active = True
                try:
                    yield
                finally:
                    self._thread_state.active = False

    def _read_private(self, name):
        path = self.state / name
        try:
            with core.regular_input(path) as fd:
                if os.name == 'nt':
                    import msvcrt
                    from . import windows_native as win
                    safe = win.trusted_handle(msvcrt.get_osfhandle(fd), private=True)
                else:
                    info = os.fstat(fd)
                    safe = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
                if not safe:
                    raise WorkspaceError('STATE_PERMISSIONS')
                value = json.loads(core.read_regular(fd, JOURNAL_BYTES))
                if isinstance(self.runner, WorkspaceRunner) and value.get('credential_authority') != 'finix:google':
                    raise WorkspaceError('MIGRATION_REQUIRED')
                return value
        except FileNotFoundError:
            return None

    def _write_private(self, name, value):
        if len(json.dumps(value).encode()) > JOURNAL_BYTES:
            raise WorkspaceError('STATE_LIMIT')
        if isinstance(self.runner, WorkspaceRunner):
            value = dict(value, credential_authority='finix:google')
        core.atomic_json(self.state / name, value)

    def _run(self, operation, *args, cancel=None, **kwargs):
        check_cancel(cancel)
        if not isinstance(self.account_digest, str) or not re.fullmatch(r'[a-f0-9]{64}', self.account_digest):
            raise WorkspaceError('RESELECTION_REQUIRED')
        if operation != 'identity':
            self._account(cancel=cancel)
        result = self.runner(operation, *args, cancel=cancel, **kwargs)
        check_cancel(cancel)
        return result

    @staticmethod
    def _transfer_progress(progress, phase):
        if progress is not None:
            progress(dict(phase=phase, completed_bytes=0, total_bytes=None, percent=None))

    def _account(self, *, cancel=None):
        account = self._run('identity', cancel=cancel).get('account_id')
        if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9_@.:-]{1,256}', account):
            raise WorkspaceError('INVALID_IDENTITY')
        digest = hashlib.sha256(account.encode()).hexdigest()
        if digest != self.account_digest:
            raise WorkspaceError('ACCOUNT_CHANGED')
        return digest

    def reconcile(self, path, *, completion=None, progress=None, cancel=None):
        """Resume the exact ciphertext; preallocated Drive IDs close lost-ACK gaps.

        Pending intent is private and durable before either folder or file create.
        Only explicit NOT_FOUND permits creation, always using the persisted ID.
        No listing/name search or overwrite is used, and reconciliation never prunes.
        """
        descriptor = cloud_files.describe(path, native=True, progress=progress, cancel=cancel, phase='checksum')
        provenance = cloud_files.completion_record(descriptor, completion)
        with self._locked():
            binding = self._binding()
            journal = self._read_private('workspace.json')
            pending = self._read_private('workspace-pending.json')
            for item in (journal, pending):
                if item is not None:
                    if not isinstance(item, dict) or item.get('profile_id') != self.owner:
                        raise WorkspaceError('BINDING_CHANGED')
                    if item.get('binding') is not None and item['binding'] != binding:
                        raise WorkspaceError('BINDING_CHANGED')
            if pending is not None:
                row = self.descriptor(pending.get('record', {}))
                if (pending.get('version') != 1 or pending.get('binding') != binding
                        or not isinstance(pending.get('account'), str)
                        or not re.fullmatch(r'[a-f0-9]{64}', pending['account'])
                        or pending.get('name') != 'finix-' + row['id'] + '.age'
                        or pending.get('folder_name') != 'Finix-' + self.owner[:12] + '-' + row['folder_id']):
                    raise WorkspaceError('INVALID_STATE')
                if any(row[k] != descriptor[k] for k in descriptor) or row.get('completion') != provenance:
                    raise WorkspaceError('PENDING_MISMATCH')
            account = self._account(cancel=cancel)
            for item in (journal, pending):
                if item is not None and item.get('account') is not None and item['account'] != account:
                    raise WorkspaceError('ACCOUNT_CHANGED')
            if journal is not None:
                if not isinstance(journal.get('records'), list):
                    raise WorkspaceError('INVALID_STATE')
                ids = set()
                for row in journal['records']:
                    self.descriptor(row)
                    if (row['folder_id'] != journal.get('folder_id') or row.get('owners') != journal.get('owners')
                            or row['id'] in ids):
                        raise WorkspaceError('INVALID_STATE')
                    ids.add(row['id'])
                self.folder_metadata(identifier(journal.get('folder_id')), journal.get('owners'),
                                     expected_name=journal.get('folder_name'), cancel=cancel)
                same = [r for r in journal['records'] if all(r[k] == descriptor[k] for k in descriptor)]
                if len(same) > 1:
                    raise WorkspaceError('AMBIGUOUS_DIGEST')
                if same:
                    row = same[0]
                    if provenance is not None and row.get('completion') != provenance:
                        raise WorkspaceError('PROVENANCE_CHANGED')
                    self.download(row, progress=progress, cancel=cancel)
                    if pending is not None and pending['record']['id'] != row['id']:
                        raise WorkspaceError('PENDING_MISMATCH')
                    check_cancel(cancel)
                    if pending is not None:
                        (self.state / 'workspace-pending.json').unlink()
                    return dict(row, verified=True, storage='workspace')
            if pending is None:
                folder = journal['folder_id'] if journal else identifier(self._run('generate-id', cancel=cancel).get('id'))
                obj = identifier(self._run('generate-id', cancel=cancel).get('id'))
                row = dict(id=obj, profile_id=self.owner, folder_id=folder,
                           owners=journal['owners'] if journal else None, **descriptor, completion=provenance,
                           account=account)
                pending = dict(version=1, profile_id=self.owner, binding=binding, account=account,
                               folder_name='Finix-' + self.owner[:12] + '-' + folder,
                               name='finix-' + obj + '.age', record=row)
                row.update(name=pending['name'], folder_name=(journal.get('folder_name') if journal else pending['folder_name']))
                if isinstance(self.runner, WorkspaceRunner):
                    row['credential_authority'] = 'finix:google'
                self._write_private('workspace-pending.json', pending)
            row = pending['record']
            # Pre-contract pending intents already bound these names privately.
            # Never infer names for committed legacy rows that lack that evidence.
            row.setdefault('name', pending['name'])
            row.setdefault('folder_name', journal.get('folder_name') if journal else pending['folder_name'])
            if journal is None:
                try:
                    self.metadata(row['folder_id'], cancel=cancel)
                except WorkspaceError as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                    self._run('create-folder', '--id', row['folder_id'], '--name', pending['folder_name'], cancel=cancel)
                owners = self.folder_metadata(row['folder_id'], row.get('owners'),
                                              expected_name=row.get('folder_name'), cancel=cancel)
                row['owners'] = owners
                self._write_private('workspace-pending.json', pending)
                journal = dict(version=2, profile_id=self.owner, binding=binding, account=account,
                               folder_id=row['folder_id'], folder_name=row.get('folder_name'), owners=owners, records=[])
                self._write_private('workspace.json', journal)
            if row['folder_id'] != journal['folder_id'] or row.get('owners') != journal['owners']:
                raise WorkspaceError('PENDING_MISMATCH')
            if len(json.dumps(dict(journal, records=[*journal['records'], row])).encode()) > JOURNAL_BYTES:
                raise WorkspaceError('STATE_LIMIT')
            exists = True
            try:
                self.metadata(row['id'], cancel=cancel)
            except WorkspaceError as exc:
                if exc.code != 'NOT_FOUND':
                    raise
                exists = False
            if not exists:
                with tempfile.TemporaryDirectory(prefix='finix-upload-', dir=str(self.state)) as stage:
                    staged = Path(stage) / 'cipher.age'
                    cloud_files.copy(path, staged, descriptor, progress=progress, cancel=cancel, phase='staging')
                    self._transfer_progress(progress, 'upload')
                    result = self._run('upload', str(staged), '--id', row['id'], '--name', pending['name'],
                                       '--parent', row['folder_id'], '--mime-type', MIME, cancel=cancel)
                    if result.get('status') != 'uploaded' or result.get('id') != row['id'] or result.get('mimeType') != MIME:
                        raise WorkspaceError('INVALID_RESPONSE')
                    cloud_files.verified(staged, descriptor, cancel=cancel)
            self.download(row, progress=progress, cancel=cancel)
            check_cancel(cancel)
            journal.update(version=2, binding=binding, account=account)
            journal['records'].append(row)
            check_cancel(cancel)
            self._write_private('workspace.json', journal)
            check_cancel(cancel)
            (self.state / 'workspace-pending.json').unlink()
            return dict(row, verified=True, storage='workspace')

    def status(self):
        # No credential introspection or authorization probes on GET.
        return {'ready': False, 'code': 'GOOGLE_AUTH_SETUP_REQUIRED', 'live_google_verified': False,
                'action': 'Use hermes auth add finix:google in this profile, then check the connection. Legacy Workspace journals require explicit migration and remain untouched.'}

    def metadata(self, obj, *, cancel=None):
        result = self._run('metadata', identifier(obj), cancel=cancel)
        if result.get('id') != obj or type(result.get('trashed')) is not bool:
            raise core.FinixError('Invalid Workspace metadata')
        return result

    def folder_metadata(self, folder, owners=None, *, expected_name=None, cancel=None):
        obj = self.metadata(folder, cancel=cancel)
        if expected_name is not None and obj.get('name') != expected_name:
            raise WorkspaceError('CLOUD_METADATA')
        actual = obj.get('owners')
        if (obj.get('mimeType') != FOLDER_MIME or obj['trashed'] or not isinstance(actual, list)
                or not actual or any(not isinstance(x.get('emailAddress'), str) for x in actual)
                or (owners is not None and actual != owners)):
            raise core.FinixError('Workspace folder identity changed')
        return actual

    def descriptor(self, row):
        if isinstance(self.runner, WorkspaceRunner) and row.get('credential_authority') != 'finix:google':
            raise WorkspaceError('MIGRATION_REQUIRED')
        if row.get('account') != self.account_digest:
            raise WorkspaceError('ACCOUNT_CHANGED')
        if (row.get('profile_id') != self.owner or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', ''))
                or type(row.get('size')) is not int or row['size'] <= 0):
            raise core.FinixError('Invalid Workspace recovery record')
        identifier(row.get('id'))
        identifier(row.get('folder_id'))
        return row

    def file_metadata(self, row, *, cancel=None):
        row = self.descriptor(row)
        obj = self.metadata(row['id'], cancel=cancel)
        if row.get('name') is not None and obj.get('name') != row['name']:
            raise WorkspaceError('CLOUD_METADATA')
        if (obj['trashed'] or obj.get('mimeType') != MIME or obj.get('parents') != [row['folder_id']]
                or str(obj.get('size')) != str(row['size']) or obj.get('owners') != row.get('owners')):
            raise core.FinixError('Workspace ownership/metadata mismatch')
        return obj

    def download(self, record, output=None, *, progress=None, cancel=None):
        record = self.descriptor(record)
        if record.get('folder_name') is not None:
            self.folder_metadata(record['folder_id'], record.get('owners'),
                                 expected_name=record['folder_name'], cancel=cancel)
        self.file_metadata(record, cancel=cancel)
        # Private ciphertext staging and exact readback prevent publication of
        # invalid media. The public SDK still buffers replies before validation;
        # this is NOT a pre-body response-byte guarantee.
        with tempfile.TemporaryDirectory(prefix='finix-workspace-', dir=os.environ.get('TMPDIR')) as stage:
            path = Path(stage) / 'cipher.age'
            self._transfer_progress(progress, 'readback')
            result = self._run('download', record['id'], '--output', str(path), expected_size=record['size'], cancel=cancel)
            if result.get('status') != 'downloaded' or result.get('id') != record['id']:
                raise core.FinixError('Workspace download response mismatch')
            return cloud_files.verified(path, record, output, progress=progress, cancel=cancel, phase='readback')

    def ledger(self):
        path = self.state / 'workspace.json'
        if not path.exists():
            # Never adopt by name after state loss: create a fresh namespace.
            result = self._run('create-folder', 'Finix-' + self.owner[:12] + '-' + uuid.uuid4().hex)
            if result.get('status') != 'created':
                raise core.FinixError('Workspace folder creation failed')
            folder = identifier(result.get('id'))
            owners = self.folder_metadata(folder)
            journal = {'profile_id': self.owner, 'folder_id': folder, 'owners': owners, 'records': []}
            core.atomic_json(path, journal)
            return journal
        journal = self._read_private('workspace.json')
        if journal.get('profile_id') != self.owner or not isinstance(journal.get('records'), list):
            raise core.FinixError('Workspace ledger binding mismatch')
        if journal.get('account') != self.account_digest:
            raise WorkspaceError('ACCOUNT_CHANGED')
        self.folder_metadata(identifier(journal.get('folder_id')), journal.get('owners'))
        ids = set()
        for row in journal['records']:
            self.descriptor(row)
            if (row['folder_id'] != journal['folder_id'] or row.get('owners') != journal['owners'] or row['id'] in ids):
                raise core.FinixError('Workspace ledger ownership mismatch')
            ids.add(row['id'])
        return journal

    def backup_cipher(self, path, keep=0, *, completion=None, progress=None, cancel=None):
        if type(keep) is not int or not 0 <= keep <= MAX_RECORDS:
            raise core.FinixError('Invalid Workspace retention')
        # All uploads, including initial capture, share the durable intent path.
        with self._locked():
            row = self.reconcile(path, completion=completion, progress=progress, cancel=cancel)
            check_cancel(cancel)
            if keep:
                self.prune(self.ledger(), keep)
            check_cancel(cancel)
            return row

    def prune(self, journal, keep):
        with self._locked():
            current = self._read_private('workspace.json')
            if current != journal:
                raise WorkspaceError('STALE_JOURNAL')
            if self._read_private('workspace-pending.json') is not None:
                raise WorkspaceError('PENDING_MISMATCH')
            if current.get('binding') is not None and current['binding'] != self._binding():
                raise WorkspaceError('BINDING_CHANGED')
            if current.get('account') is not None and current['account'] != self._account():
                raise WorkspaceError('ACCOUNT_CHANGED')
            self._prune_locked(journal, keep)

    def _prune_locked(self, journal, keep):
        for old in cloud_files.retention_candidates(journal['records'], keep):
            try:
                obj = self.metadata(old['id'])
            except WorkspaceError as exc:
                if exc.code != 'NOT_FOUND':
                    raise
                obj = None
            if obj is not None and not obj['trashed']:
                self.download(old)  # fresh ownership, parent, size, and SHA readback
                result = self._run('delete', old['id'])  # reversible trash only
                if (result.get('status') != 'trashed' or result.get('fileId') != old['id']
                        or result.get('permanent') is not False):
                    raise core.FinixError('Workspace trash failed')
                if not self.metadata(old['id'])['trashed']:
                    raise core.FinixError('Workspace trash readback failed')
            journal['records'].remove(old)
            core.atomic_json(self.state / 'workspace.json', journal)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'download'])
    parser.add_argument('--home', required=True)
    parser.add_argument('--profile-id', required=True)
    parser.add_argument('--record', help='Public verified upload record JSON; no credentials or identity')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    try:
        from .integration import Profile
        selected_account = Profile(args.home).config().get('workspace_account')
        drive = WorkspaceDrive(args.home, args.profile_id, Path(args.home) / 'unused-finix-recovery-state',
                               account_digest=selected_account)
        if args.command == 'status':
            result = drive.status()
            print(json.dumps(result))
            return 0 if result['ready'] else 2
        if not args.record or not args.output:
            parser.error('download requires --record and --output')
        with core.regular_input(args.record) as fd:
            record = json.loads(core.read_regular(fd, 65536))
        drive.download(record, args.output)
        print(json.dumps({'verified': True, 'sha256': record['sha256'], 'output': args.output}))
        return 0
    except (core.FinixError, OSError, ValueError, KeyError, TypeError):
        print('finix workspace: operation failed; provider diagnostics withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
