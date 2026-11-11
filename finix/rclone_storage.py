"""Optional existing-account rclone storage; no OAuth, config parsing or auto-install.

The owner-selected rclone config is trusted executable configuration. Finix reads
only its filesystem metadata; rclone owns refresh and credentials. The private
ledger authorizes exact random object paths, never prefix/list-based deletion.
"""

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import core
from . import cloud_files
from .streaming import check_cancel


MAX_RECORDS = 1000
CLOUD_TYPES = {'yandex', 'onedrive', 'dropbox'}


ACTIONS = {
    'AUTH_REQUIRED': 'Existing authorization was rejected. Ask the owning account administrator to repair it; no interactive login attempted.',
    'ACCESS_DENIED': 'The provider denied access. Check the existing account permissions for the selected namespace.',
    'QUOTA_EXCEEDED': 'The provider reports insufficient storage. Free space or increase quota, then reconcile the same ciphertext.',
    'RATE_LIMITED': 'The provider is rate limiting. Wait, then reconcile the same ciphertext.',
    'CONNECTION_FAILED': 'Connection failed. Check connectivity, DNS, proxy and firewall, then reconcile the same ciphertext.',
    'TIMEOUT': 'The operation timed out. Check connectivity and reconcile the same ciphertext; remote upload may already exist.',
    'PENDING_RECONCILIATION': 'An upload is pending. Reconcile the same ciphertext and object identity before creating another snapshot.',
    'LEGACY_NAMESPACE_REQUIRED': 'Preserve or recover the original ledger namespace; account-wide search is not allowed.',
    'LEGACY_NO_MATCH': 'No full-hash match for the selected object. Preserve ciphertext and remote objects; no overwrite attempted.',
    'PROVIDER_ERROR': 'Provider failure could not be classified safely. Check existing account authorization, quota and connectivity; preserve ciphertext and reconcile before retrying.',
}
KNOWN_CODES = set(ACTIONS) | {
    'CONFIG_PERMISSIONS', 'CONFIG_UNAVAILABLE', 'BINARY_MISSING', 'UNTRUSTED_BINARY',
    'NOT_FOUND', 'OUTPUT_LIMIT', 'SHORT_DOWNLOAD', 'INVALID_RESPONSE', 'REMOTE_UNAVAILABLE',
    'SELECTION_REQUIRED', 'STATE_PERMISSIONS',
}


class RcloneError(core.FinixError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in KNOWN_CODES else 'PROVIDER_ERROR'
        action = ACTIONS.get(self.code, 'Check the explicit binary, selected config permissions and remote namespace; preserve local ciphertext. No login or fallback attempted.')
        super().__init__('Rclone ' + self.code + ': ' + action)


def failure_code(returncode, stderr=b''):
    """Never inspect free-form msg/error strings or infer HTTP codes from them.

    Standard rclone JSON logs often have *only* a message. Those stay unknown.
    A numeric top-level HTTP status, when supplied by a structured transport,
    can narrow the diagnostic. Conflicting/malformed data stays unknown.
    """
    if returncode in {3, 4}:
        return 'NOT_FOUND'
    if returncode == 10:
        return 'TIMEOUT'
    if not isinstance(stderr, bytes) or len(stderr) > 65536:
        return 'PROVIDER_ERROR'
    statuses, network_errors = set(), set()
    try:
        for line in stderr.splitlines():
            row = json.loads(line)
            if not isinstance(row, dict):
                return 'PROVIDER_ERROR'
            status = row.get('status')
            number = row.get('errno')
            if number is not None:
                if type(number) is not int:
                    return 'PROVIDER_ERROR'
                network_errors.add(number)
            if status is not None:
                if type(status) is not int:
                    return 'PROVIDER_ERROR'
                statuses.add(status)
    except (ValueError, UnicodeError):
        return 'PROVIDER_ERROR'
    if not statuses and network_errors and network_errors <= {
            errno.ECONNREFUSED, errno.ECONNRESET, errno.ENETUNREACH,
            errno.EHOSTUNREACH, errno.ENETDOWN, 10061, 10054, 10051, 10065, 10050}:
        return 'CONNECTION_FAILED'
    if len(statuses) != 1 or network_errors:
        return 'PROVIDER_ERROR'
    return {401: 'AUTH_REQUIRED', 403: 'ACCESS_DENIED', 507: 'QUOTA_EXCEEDED',
            408: 'TIMEOUT', 504: 'TIMEOUT', 429: 'RATE_LIMITED'}.get(statuses.pop(), 'PROVIDER_ERROR')


def remote_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', value):
        raise core.FinixError('Expected a normal configured rclone remote name without colon/options')
    return value


def relative_root(value):
    if (not isinstance(value, str) or len(value) > 500 or not value
            or any(not re.fullmatch(r'[A-Za-z0-9_ .-]{1,100}', p) or p in {'.', '..'}
                   or p != p.strip() for p in value.split('/'))):
        raise core.FinixError('Expected a nonempty normalized relative rclone root')
    return value


def config_path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise core.FinixError('Explicit absolute rclone config path required')
    path = core.checked_path(value)
    if os.name == 'nt':
        import msvcrt
        from . import windows_native as win
        try:
            with win.directory(path.parent) as parent, win.regular(path) as fd:
                if not win.trusted_handle(parent.handle, private=True) or not win.trusted_handle(msvcrt.get_osfhandle(fd), private=True):
                    raise RcloneError('CONFIG_PERMISSIONS')
        except OSError:
            raise RcloneError('CONFIG_UNAVAILABLE') from None
        return path
    try:
        info, parent = path.stat(), path.parent.stat()
    except OSError:
        raise RcloneError('CONFIG_UNAVAILABLE') from None
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or stat.S_IMODE(parent.st_mode) != 0o700
            or info.st_uid != os.getuid() or parent.st_uid != os.getuid()):
        raise RcloneError('CONFIG_PERMISSIONS')
    return path


def child_env(home):
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if k in os.environ}
    if os.name == 'nt':
        for key in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'):
            if key in os.environ:
                env[key] = os.environ[key]
    env['HERMES_HOME'] = str(home)
    return env


class _TransferOutput:
    """Bounded line framing: routine JSON statistics are not diagnostics."""
    def __init__(self, operation, output, expected_size, progress):
        self.operation, self.output = operation, output
        self.expected_size, self.progress = expected_size, progress
        self.data, self.diagnostic, self.line = bytearray(), bytearray(), bytearray()
        self.total, self.completed = 0, 0
        self.phase = 'upload' if operation == 'copyto' else 'verify'
        self.tracked = operation in {'copyto', 'cat'} and type(expected_size) is int and expected_size > 0
        if self.tracked:
            self.report(0)

    def report(self, count, *, done=False):
        if self.progress is not None:
            self.progress({'phase': self.phase, 'completed_bytes': count,
                           'total_bytes': self.expected_size,
                           'percent': 100 if done else min(99, count * 100 // self.expected_size)})

    def _line(self):
        raw = bytes(self.line).rstrip(b'\r\n')
        self.line.clear()
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            row = None
        stats = row.get('stats') if isinstance(row, dict) else None
        if isinstance(stats, dict):
            count, total = stats.get('bytes'), stats.get('totalBytes')
            # Use only exact integer counters for this frozen ciphertext; never
            # parse human messages, transferring names, rates or credentials.
            if (self.operation == 'copyto' and self.tracked
                    and type(count) is int and type(total) is int
                    and total == self.expected_size and self.completed <= count <= total
                    and count != self.completed):
                self.completed = count
                self.report(count)
            return
        if len(self.diagnostic) + len(raw) + 1 > 65536:
            raise RcloneError('OUTPUT_LIMIT')
        self.diagnostic.extend(raw + b'\n')

    def feed(self, stdout, chunk):
        if stdout:
            self.total += len(chunk)
            limit = self.expected_size if self.operation == 'cat' else 65536
            if self.total > limit:
                raise RcloneError('OUTPUT_LIMIT')
            if self.operation == 'cat':
                self.output.write(chunk)
                if self.tracked:
                    self.report(self.total)
            else:
                self.data.extend(chunk)
        else:
            for part in chunk.splitlines(keepends=True):
                if len(self.line) + len(part) > 65536:
                    raise RcloneError('OUTPUT_LIMIT')
                self.line.extend(part)
                if part.endswith(b'\n'):
                    self._line()

    def finish(self, returncode):
        if self.line:
            self._line()
        if returncode:
            raise RcloneError(failure_code(returncode, bytes(self.diagnostic)))
        if self.operation == 'cat' and self.total != self.expected_size:
            raise RcloneError('SHORT_DOWNLOAD')
        # cat has not passed the adapter's full hash check yet.
        if self.operation == 'copyto' and self.tracked:
            self.report(self.expected_size, done=True)
        return bytes(self.data)


class RcloneRunner:
    def __init__(self, home, config, *, binary=None, timeout=120, transfer_timeout=86400):
        self.home, self.config = core.checked_path(home), str(config)
        if binary is None and os.name == 'nt':
            from .windows_native import find_tool
            try:
                found = find_tool('rclone')
            except FileNotFoundError:
                found = None
            except OSError:
                raise RcloneError('UNTRUSTED_BINARY') from None
        else:
            found = binary if binary else shutil.which('rclone')
        self.binary = str(core.checked_path(found)) if found and Path(found).is_absolute() else None
        self.timeout = timeout
        self.transfer_timeout = transfer_timeout

    def __call__(self, operation, *args, output=None, expected_size=None, progress=None, cancel=None):
        check_cancel(cancel)
        if operation not in {'listremotes', 'copyto', 'cat', 'deletefile', 'lsjson'}:
            raise core.FinixError('Unsupported rclone operation')
        if not self.binary or not Path(self.binary).is_file() or not os.access(self.binary, os.X_OK):
            raise RcloneError('BINARY_MISSING')
        if os.name == 'nt':
            from .windows_native import trusted_binary
            trusted = trusted_binary(self.binary)
        else:
            info = Path(self.binary).stat()
            trusted = info.st_uid in {0, os.getuid()} and not info.st_mode & 0o022
        if not trusted:
            raise RcloneError('UNTRUSTED_BINARY')
        cfg = config_path(self.config)
        # Never inherit credential overrides or invoke interactive authorization.
        argv = [self.binary, '--config', str(cfg), '--ask-password=false',
                '--retries', '1', '--low-level-retries', '1', '--contimeout', '15s',
                '--timeout', '60s', '--log-level', 'ERROR', '--use-json-log']
        if operation == 'copyto' and progress is not None:
            # Match ERROR filtering: NOTICE/INFO stats would be suppressed.
            argv += ['--stats=1s', '--stats-log-level=ERROR']
        argv += [operation, *map(str, args)]
        if operation == 'cat' and (output is None or type(expected_size) is not int or expected_size <= 0):
            raise core.FinixError('Rclone cat requires file sink and exact declared size')
        if os.name == 'nt':
            return self._windows_call(argv, operation, output, expected_size, progress, cancel)
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=child_env(self.home), start_new_session=True)
        def cleanup():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        return self._consume(process, operation, output, expected_size, progress, cancel,
                             self._posix_events, cleanup)

    @staticmethod
    def _posix_events(process, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                for key, _ in selector.select(.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        yield key.data, chunk

    def _consume(self, process, operation, output, expected_size, progress, cancel, events, cleanup):
        deadline = time.monotonic() + (self.transfer_timeout if operation in {'cat', 'copyto'} else self.timeout)
        stopped, cleanup_lock = threading.Event(), threading.Lock()
        cancelled, cleaned = [], False

        def cleanup_once():
            nonlocal cleaned
            with cleanup_lock:
                if not cleaned:
                    cleanup()
                    cleaned = True

        def watch_cancel():
            # output_events may be waiting on a completely silent child. The
            # same monitor handles pipes-closed/process-alive waits on POSIX.
            while not stopped.wait(.05):
                try:
                    check_cancel(cancel)
                except BaseException as exc:
                    cancelled.append(exc)
                    cleanup_once()
                    return

        watcher = threading.Thread(target=watch_cancel, daemon=True) if cancel is not None else None
        try:
            if watcher is not None:
                watcher.start()
            stream = _TransferOutput(operation, output, expected_size, progress)
            for stdout, chunk in events(process, deadline):
                if cancelled:
                    raise cancelled[0]
                check_cancel(cancel)
                stream.feed(stdout, chunk)
            process.wait(timeout=max(.01, deadline - time.monotonic()))
            if cancelled:
                raise cancelled[0]
            check_cancel(cancel)
            return stream.finish(process.returncode)
        except (TimeoutError, subprocess.TimeoutExpired):
            if cancelled:
                raise cancelled[0]
            check_cancel(cancel)
            raise RcloneError('TIMEOUT') from None
        finally:
            stopped.set()
            cleanup_once()
            if watcher is not None and watcher.ident is not None:
                watcher.join()
            process.stdout.close()
            process.stderr.close()

    def _windows_call(self, argv, operation, output, expected_size, progress=None, cancel=None):
        from . import windows_native as win
        if operation == 'cat' and (output is None or type(expected_size) is not int or expected_size <= 0):
            raise core.FinixError('Rclone cat requires file sink and exact declared size')
        with win.regular(self.binary), win.directory(Path(self.config).parent):
            process = win.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=child_env(self.home))
            return self._consume(process, operation, output, expected_size, progress, cancel,
                                 win.output_events, lambda: win.cleanup(process))


class RcloneStorage:
    def __init__(self, home, profile_id, state, config, remote, root, *, runner=None):
        if not isinstance(profile_id, str) or not re.fullmatch(r'[a-f0-9]{64}', profile_id):
            raise core.FinixError('Invalid Finix profile binding')
        self.home, self.owner, self.state = core.checked_path(home), profile_id, core.checked_path(state)
        self.config = config_path(config)
        self.remote, self.root = remote_name(remote), relative_root(root)
        self.runner = runner or RcloneRunner(self.home, self.config)
        self._operation_lock = threading.RLock()
        self._thread_state = threading.local()

    def _run(self, operation, *args, progress=None, cancel=None, **kwargs):
        check_cancel(cancel)
        # Preserve injected legacy runner signatures. Do not hide a runner's
        # own TypeError by attempting a compatibility retry.
        if progress is not None:
            kwargs['progress'] = progress
        if cancel is not None:
            kwargs['cancel'] = cancel
        result = self.runner(operation, *args, **kwargs)
        check_cancel(cancel)
        return result

    def selected_type(self, cancel=None):
        config_path(str(self.config))
        try:
            remotes = json.loads(self._run('listremotes', '--json', cancel=cancel))
        except (ValueError, UnicodeError):
            raise RcloneError('INVALID_RESPONSE') from None
        if not isinstance(remotes, list):
            raise RcloneError('INVALID_RESPONSE')
        selected = [r for r in remotes if isinstance(r, dict) and r.get('name') == self.remote and r.get('source') == 'file']
        if len(selected) != 1 or selected[0].get('type') not in CLOUD_TYPES:
            raise RcloneError('REMOTE_UNAVAILABLE')
        return selected[0]['type']

    def status(self):
        try:
            kind = self.selected_type()
            return {'ready': True, 'code': 'LOCAL_READY', 'type': kind, 'live_cloud_verified': False,
                    'action': 'Local config discovery only; account/permissions checked on explicit backup. New local login is available in the authenticated Finix UI.'}
        except RcloneError as exc:
            return {'ready': False, 'code': exc.code, 'live_cloud_verified': False, 'action': str(exc)}

    def binding(self, cancel=None):
        parent = self.config.parent.stat()
        # OAuth refresh may atomically replace the file. Bind its fixed private
        # parent identity instead, never a token fingerprint/read/copy.
        return {'config': str(self.config), 'parent_device': parent.st_dev, 'parent_inode': parent.st_ino,
                'remote': self.remote, 'root': self.root, 'type': self.selected_type(cancel=cancel)}

    def descriptor(self, row):
        if (not isinstance(row, dict) or row.get('profile_id') != self.owner
                or row.get('remote') != self.remote or row.get('root') != self.root
                or row.get('type') not in CLOUD_TYPES
                or not re.fullmatch(r'[a-f0-9]{32}', row.get('namespace', ''))
                or not re.fullmatch(r'[a-f0-9]{32}\.age', row.get('id', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', ''))
                or type(row.get('size')) is not int or row['size'] <= 0):
            raise core.FinixError('Invalid rclone recovery/ownership record')
        return row

    def object_path(self, row):
        self.descriptor(row)
        return self.remote + ':' + self.root + '/' + row['namespace'] + '/' + row['id']

    def download(self, record, output=None, progress=None, cancel=None):
        check_cancel(cancel)
        self.descriptor(record)
        if self.selected_type(cancel=cancel) != record['type']:
            raise core.FinixError('Rclone remote type changed')
        # cat also accepts directories; stat must establish an exact file first.
        try:
            metadata = json.loads(self._run('lsjson', self.object_path(record), '--stat', cancel=cancel))
        except (ValueError, UnicodeError):
            raise RcloneError('INVALID_RESPONSE') from None
        if (not isinstance(metadata, dict) or metadata.get('IsDir') is not False
                or metadata.get('Size') != record['size'] or metadata.get('Name') != record['id']):
            raise core.FinixError('Rclone object metadata mismatch')
        with tempfile.TemporaryDirectory(prefix='finix-rclone-read-', dir=os.environ.get('TMPDIR')) as stage:
            path = Path(stage) / 'cipher.age'
            with core.regular_output(path) as fd, os.fdopen(os.dup(fd), 'wb') as stream:
                self._run('cat', self.object_path(record), output=stream, expected_size=record['size'],
                          progress=progress, cancel=cancel)
            result = cloud_files.verified(path, record, output, native=True, cancel=cancel)
            check_cancel(cancel)
            if progress is not None:
                progress(dict(phase='verify', completed_bytes=record['size'], total_bytes=record['size'], percent=100))
            return result

    @contextmanager
    def _locked(self):
        # Serialize this adapter's threads before taking the cross-process lock.
        # Only the owning thread may reuse its pinned anchor for nested calls.
        with self._operation_lock:
            if hasattr(self._thread_state, 'anchor'):
                yield
                return
            with self._state_locked():
                yield

    @property
    def _anchor(self):
        return self._thread_state.anchor

    @contextmanager
    def _state_locked(self):
        core.private_dir(self.state)
        with core.locked(self.state) as anchor:
            if os.name == 'nt':
                from . import windows_native as win
                safe = win.trusted_handle(anchor.handle, private=True)
            else:
                info = os.fstat(anchor)
                safe = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
            if not safe:
                raise RcloneError('STATE_PERMISSIONS')
            self._thread_state.anchor = anchor
            try:
                yield
            finally:
                del self._thread_state.anchor

    def _read(self, name, limit):
        try:
            if os.name == 'nt':
                import msvcrt
                from . import windows_native as win
                with core.regular_input(self.state / name) as fd:
                    if not win.trusted_handle(msvcrt.get_osfhandle(fd), private=True):
                        raise RcloneError('STATE_PERMISSIONS')
                    return json.loads(core.read_regular(fd, limit))
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._anchor)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                    raise RcloneError('STATE_PERMISSIONS')
                return json.loads(core.read_regular(fd, limit))
            finally:
                os.close(fd)
        except FileNotFoundError:
            return None

    def _write(self, name, data):
        if os.name == 'nt':
            # The directory handle pins all ancestors against rename/reparse.
            core.atomic_json(self.state / name, data)
            return
        temp = '.finix-' + uuid.uuid4().hex
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._anchor)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, name, src_dir_fd=self._anchor, dst_dir_fd=self._anchor)
            os.fsync(self._anchor)
        finally:
            try:
                os.unlink(temp, dir_fd=self._anchor)
            except FileNotFoundError:
                pass

    def _clear_pending(self):
        if os.name == 'nt':
            (self.state / 'rclone-pending.json').unlink()
        else:
            os.unlink('rclone-pending.json', dir_fd=self._anchor)
            os.fsync(self._anchor)

    def ledger(self, cancel=None):
        with self._locked():
            return self._ledger_locked(cancel=cancel)

    def _ledger_locked(self, cancel=None):
        binding = self.binding(cancel=cancel)
        journal = self._read('rclone.json', 1024 * 1024)
        if journal is None:
            journal = {'profile_id': self.owner, 'binding': binding, 'namespace': uuid.uuid4().hex, 'records': []}
            self._write('rclone.json', journal)
            return journal
        if (journal.get('profile_id') != self.owner or journal.get('binding') != binding
                or not isinstance(journal.get('records'), list)
                or len(journal['records']) > MAX_RECORDS
                or not re.fullmatch(r'[a-f0-9]{32}', journal.get('namespace', ''))):
            raise core.FinixError('Rclone ledger/config/profile binding changed; preserve old ledger and start a new namespace')
        ids = set()
        for row in journal['records']:
            self.descriptor(row)
            if row['namespace'] != journal['namespace'] or row['type'] != binding['type'] or row['id'] in ids:
                raise core.FinixError('Rclone ledger ownership mismatch')
            ids.add(row['id'])
        return journal

    def pending(self, journal):
        """Private durable intent, deliberately excludes credential/source paths."""
        intent = self._read('rclone-pending.json', 65536)
        if intent is None:
            return None
        row = self.descriptor(intent['record'])
        binding = hashlib.sha256(json.dumps(journal['binding'], sort_keys=True).encode()).hexdigest()
        if (intent.get('version') != 1 or intent.get('binding_sha256') != binding
                or row['namespace'] != journal['namespace'] or row['type'] != journal['binding']['type']
                or cloud_files.completion_record(row, row.get('completion')) != row.get('completion')):
            raise core.FinixError('Rclone pending intent binding changed')
        return row

    def _intent(self, journal, row):
        self._write('rclone-pending.json', {
            'version': 1, 'binding_sha256': hashlib.sha256(
                json.dumps(journal['binding'], sort_keys=True).encode()).hexdigest(), 'record': row})

    def _reconcile(self, journal, row, path=None, progress=None, cancel=None):
        # Only definite absence permits a copy. Unknown errors/mismatch never
        # authorize overwrite, deletion, a different name or a fresh snapshot.
        try:
            self.download(row, progress=progress, cancel=cancel)
        except RcloneError as exc:
            if exc.code != 'NOT_FOUND' or path is None:
                raise
            cloud_files.verified(path, row, native=True, cancel=cancel)
            with tempfile.TemporaryDirectory(prefix='finix-rclone-', dir=os.environ.get('TMPDIR')) as stage:
                staged = Path(stage) / 'cipher.age'
                cloud_files.copy(path, staged, {key: row[key] for key in ('size', 'sha256')}, cancel=cancel)
                options = {'expected_size': row['size']} if progress is not None else {}
                self._run('copyto', str(staged), self.object_path(row), '--immutable',
                          progress=progress, cancel=cancel, **options)
                cloud_files.verified(staged, row, native=True, cancel=cancel)
            self.download(row, progress=progress, cancel=cancel)
        check_cancel(cancel)
        existing = [r for r in journal['records'] if r['id'] == row['id']]
        if existing:
            if existing != [row]:
                raise core.FinixError('Rclone pending/committed identity conflict')
        else:
            if len(journal['records']) >= MAX_RECORDS:
                raise core.FinixError('Rclone ledger full; pending intent preserved')
            journal['records'].append(row)
            self._write('rclone.json', journal)
        self._clear_pending()
        return dict(row, verified=True, storage='rclone')

    def require_no_pending(self):
        """Call before source capture, under the owning profile operation lock."""
        with self._locked():
            journal = self.ledger()
            if self.pending(journal) is not None:
                raise RcloneError('PENDING_RECONCILIATION')

    def reconcile(self, path=None, progress=None, cancel=None):
        """Resume only the durable object identity; never builds a snapshot/prunes."""
        check_cancel(cancel)
        with self._locked():
            if self._read('rclone.json', 1024 * 1024) is None:
                raise RcloneError('LEGACY_NAMESPACE_REQUIRED')
            journal = self.ledger(cancel=cancel)
            row = self.pending(journal)
            if row is None:
                raise core.FinixError('No rclone pending intent; use explicit legacy reconciliation')
            return self._reconcile(journal, row, path, progress=progress, cancel=cancel)

    def reconcile_legacy(self, path, *, object_id=None, completion=None):
        """Read-only scoped search; explicit exact id adopts only full-hash matches.

        Requires the original ledger namespace. A missing ledger never permits
        root/account scans. Unknown historical completeness stays unknown.
        """
        if object_id is not None and (not isinstance(object_id, str) or not re.fullmatch(r'[a-f0-9]{32}\.age', object_id)):
            raise core.FinixError('Invalid legacy object identity')
        with self._locked():
            if self._read('rclone.json', 1024 * 1024) is None:
                raise RcloneError('LEGACY_NAMESPACE_REQUIRED')
            journal = self.ledger()
            if self.pending(journal) is not None:
                raise RcloneError('PENDING_RECONCILIATION')
            descriptor = cloud_files.describe(path, native=True)
            provenance = cloud_files.completion_record(descriptor, completion)
            base = self.remote + ':' + self.root + '/' + journal['namespace']
            raw = self.runner('lsjson', base, '--files-only', '--max-depth', '1')
            if len(raw) > 65536:
                raise RcloneError('OUTPUT_LIMIT')
            try:
                listing = json.loads(raw)
            except (ValueError, UnicodeError):
                raise RcloneError('INVALID_RESPONSE') from None
            if not isinstance(listing, list) or len(listing) > MAX_RECORDS:
                raise RcloneError('OUTPUT_LIMIT')
            matches, seen = [], set()
            for entry in listing:
                if not isinstance(entry, dict):
                    raise RcloneError('INVALID_RESPONSE')
                name = entry.get('Name')
                if (entry.get('IsDir') is not False or type(entry.get('Size')) is not int
                        or entry['Size'] != descriptor['size'] or not isinstance(name, str)
                        or not re.fullmatch(r'[a-f0-9]{32}\.age', name)):
                    continue
                if name in seen:
                    raise RcloneError('INVALID_RESPONSE')
                seen.add(name)
                if object_id is not None and name != object_id:
                    continue
                row = dict(profile_id=self.owner, remote=self.remote, root=self.root,
                           type=journal['binding']['type'], namespace=journal['namespace'],
                           id=name, completion=provenance, **descriptor)
                try:
                    self.download(row)
                except core.FinixError as exc:
                    # Only an exact internally generated hash mismatch is a
                    # non-match; auth/network/metadata failures remain closed.
                    if type(exc) is core.FinixError and str(exc) == 'Ciphertext readback mismatch; retention not run':
                        continue
                    raise
                matches.append(row)
            if object_id is None:
                return matches
            if len(matches) != 1:
                raise RcloneError('LEGACY_NO_MATCH')
            self._intent(journal, matches[0])
            # No source argument: legacy adoption can never perform a copy.
            return self._reconcile(journal, matches[0])

    def backup_cipher(self, path, keep=0, completion=None, progress=None, cancel=None):
        check_cancel(cancel)
        if type(keep) is not int or not 0 <= keep <= MAX_RECORDS:
            raise core.FinixError('Invalid rclone retention')
        with self._locked():
            descriptor = cloud_files.describe(path, native=True, cancel=cancel)
            provenance = cloud_files.completion_record(descriptor, completion)
            journal = self.ledger(cancel=cancel)
            if self.pending(journal) is not None:
                raise RcloneError('PENDING_RECONCILIATION')
            if len(journal['records']) >= MAX_RECORDS:
                raise core.FinixError('Rclone ledger full; no upload')
            row = dict(profile_id=self.owner, remote=self.remote, root=self.root, type=journal['binding']['type'],
                       namespace=journal['namespace'], id=uuid.uuid4().hex + '.age',
                       completion=provenance, **descriptor)
            self._intent(journal, row)
            result = self._reconcile(journal, row, path, progress=progress, cancel=cancel)
            if keep:
                if cancel is None:
                    self.prune(journal, keep)
                else:
                    self.prune(journal, keep, cancel=cancel)
            return result

    def prune(self, journal, keep, cancel=None):
        check_cancel(cancel)
        with self._locked():
            persisted = self._ledger_locked(cancel=cancel)
            if journal != persisted:
                raise core.FinixError('Rclone retention ledger changed')
            return self._prune_locked(persisted, keep, cancel=cancel)

    def _prune_locked(self, journal, keep, cancel=None):
        if not cloud_files.retention_candidates(journal['records'], keep):
            return
        if self.pending(journal) is not None:
            raise RcloneError('PENDING_RECONCILIATION')
        for row in cloud_files.retention_candidates(journal['records'], keep):
            check_cancel(cancel)
            try:
                self.download(row, cancel=cancel)
            except RcloneError as exc:
                if exc.code != 'NOT_FOUND':
                    raise
            else:
                self._run('deletefile', self.object_path(row), cancel=cancel)
                try:
                    self.download(row, cancel=cancel)
                except RcloneError as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                else:
                    raise core.FinixError('Rclone deletion readback failed')
            journal['records'].remove(row)
            self._write('rclone.json', journal)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'download', 'reconcile', 'legacy-scan', 'legacy-adopt'])
    parser.add_argument('--home', required=True)
    parser.add_argument('--profile-id', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--remote', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--record')
    parser.add_argument('--output')
    parser.add_argument('--state', help='Exact owning rclone ledger directory; required for reconciliation')
    parser.add_argument('--cipher', help='Retained native age ciphertext; never a source profile')
    parser.add_argument('--object-id', help='Explicit legacy object id to adopt after full verification')
    args = parser.parse_args(argv)
    try:
        if args.command in {'reconcile', 'legacy-scan', 'legacy-adopt'} and not args.state:
            parser.error('reconciliation requires --state')
        adapter = RcloneStorage(args.home, args.profile_id, args.state or Path(args.home) / 'unused-rclone-recovery',
                                args.config, args.remote, args.root)
        if args.command == 'status':
            result = adapter.status()
            print(json.dumps(result))
            return 0 if result['ready'] else 2
        if args.command == 'reconcile':
            print(json.dumps(adapter.reconcile(args.cipher)))
            return 0
        if args.command in {'legacy-scan', 'legacy-adopt'}:
            if not args.cipher or (args.command == 'legacy-adopt' and not args.object_id):
                parser.error('legacy reconciliation requires --cipher; adoption also requires --object-id')
            print(json.dumps(adapter.reconcile_legacy(args.cipher,
                object_id=args.object_id if args.command == 'legacy-adopt' else None)))
            return 0
        if not args.record or not args.output:
            parser.error('download requires --record and --output')
        with core.regular_input(core.checked_path(args.record)) as fd:
            record = json.loads(core.read_regular(fd, 65536))
        adapter.download(record, args.output)
        print(json.dumps({'verified': True, 'sha256': record['sha256'], 'output': args.output}))
        return 0
    except RcloneError as exc:
        print(json.dumps({'code': exc.code, 'action': str(exc)}), file=sys.stderr)
        return 1
    except (core.FinixError, OSError, ValueError, KeyError, TypeError):
        print('finix rclone: operation failed; provider diagnostics withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
