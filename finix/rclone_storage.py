"""Optional existing-account rclone storage; no OAuth, config parsing or auto-install.

The owner-selected rclone config is trusted executable configuration. Finix reads
only its filesystem metadata; rclone owns refresh and credentials. The private
ledger authorizes exact random object paths, never prefix/list-based deletion.
"""
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
import time
import uuid

from . import core

MAX_CIPHER = 129 * 1024 * 1024
MAX_RECORDS = 1000
CLOUD_TYPES = {'yandex', 'onedrive', 'dropbox'}


class RcloneError(core.FinixError):
    def __init__(self, code):
        self.code = code
        super().__init__('Rclone ' + code + ': check explicit binary/config/remote/root, existing authorization and network. Encrypted configs need an external secure unlock workflow (not supported here); no login or fallback attempted.')


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
    env['HERMES_HOME'] = str(home)
    return env


class RcloneRunner:
    def __init__(self, home, config, *, binary=None, timeout=120):
        self.home, self.config = core.checked_path(home), str(config)
        found = binary if binary else shutil.which('rclone')
        self.binary = str(core.checked_path(found)) if found and Path(found).is_absolute() else None
        self.timeout = timeout

    def __call__(self, operation, *args):
        if operation not in {'listremotes', 'copyto', 'cat', 'deletefile', 'lsjson'}:
            raise core.FinixError('Unsupported rclone operation')
        if not self.binary or not Path(self.binary).is_file() or not os.access(self.binary, os.X_OK):
            raise RcloneError('BINARY_MISSING')
        info = Path(self.binary).stat()
        if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
            raise RcloneError('UNTRUSTED_BINARY')
        cfg = config_path(self.config)
        # Fixed argv only; no shell, stdin prompts, inherited RCLONE_* overrides,
        # environment credentials, arbitrary flags, daemon, sync or remote scans.
        argv = [self.binary, '--config', str(cfg), '--ask-password=false',
                '--retries', '1', '--low-level-retries', '1', '--contimeout', '15s',
                '--timeout', '60s', '--log-level', 'ERROR', operation, *map(str, args)]
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=child_env(self.home), start_new_session=True)
        assert process.stdout is not None and process.stderr is not None
        data, total = bytearray(), 0
        limit = MAX_CIPHER if operation == 'cat' else 65536
        deadline = time.monotonic() + self.timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                errors = 0
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise RcloneError('TIMEOUT')
                    for key, _ in selector.select(.05):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data:
                            total += len(chunk)
                            if total > limit:
                                raise RcloneError('OUTPUT_LIMIT')
                            data.extend(chunk)
                        else:
                            errors += len(chunk)
                            if errors > 65536:
                                raise RcloneError('OUTPUT_LIMIT')
                process.wait(timeout=max(.01, deadline - time.monotonic()))
            if process.returncode:
                raise RcloneError('NOT_FOUND' if process.returncode in {3, 4} else 'PROVIDER_ERROR')
            return bytes(data)
        except subprocess.TimeoutExpired:
            raise RcloneError('TIMEOUT') from None
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


class RcloneStorage:
    def __init__(self, home, profile_id, state, config, remote, root, *, runner=None):
        if not isinstance(profile_id, str) or not re.fullmatch(r'[a-f0-9]{64}', profile_id):
            raise core.FinixError('Invalid Finix profile binding')
        self.home, self.owner, self.state = core.checked_path(home), profile_id, core.checked_path(state)
        self.config = config_path(config)
        self.remote, self.root = remote_name(remote), relative_root(root)
        self.runner = runner or RcloneRunner(self.home, self.config)

    def selected_type(self):
        config_path(str(self.config))
        try:
            remotes = json.loads(self.runner('listremotes', '--json'))
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

    def binding(self):
        parent = self.config.parent.stat()
        # OAuth refresh may atomically replace the file. Bind its fixed private
        # parent identity instead, never a token fingerprint/read/copy.
        return {'config': str(self.config), 'parent_device': parent.st_dev, 'parent_inode': parent.st_ino,
                'remote': self.remote, 'root': self.root, 'type': self.selected_type()}

    def descriptor(self, row):
        if (not isinstance(row, dict) or row.get('profile_id') != self.owner
                or row.get('remote') != self.remote or row.get('root') != self.root
                or row.get('type') not in CLOUD_TYPES
                or not re.fullmatch(r'[a-f0-9]{32}', row.get('namespace', ''))
                or not re.fullmatch(r'[a-f0-9]{32}\.age', row.get('id', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', ''))
                or type(row.get('size')) is not int or not 0 < row['size'] <= MAX_CIPHER):
            raise core.FinixError('Invalid rclone recovery/ownership record')
        return row

    def object_path(self, row):
        self.descriptor(row)
        return self.remote + ':' + self.root + '/' + row['namespace'] + '/' + row['id']

    def download(self, record, output=None):
        self.descriptor(record)
        if self.selected_type() != record['type']:
            raise core.FinixError('Rclone remote type changed')
        # cat also accepts directories; stat must establish an exact file first.
        try:
            metadata = json.loads(self.runner('lsjson', self.object_path(record), '--stat'))
        except (ValueError, UnicodeError):
            raise RcloneError('INVALID_RESPONSE') from None
        if (not isinstance(metadata, dict) or metadata.get('IsDir') is not False
                or metadata.get('Size') != record['size'] or metadata.get('Name') != record['id']):
            raise core.FinixError('Rclone object metadata mismatch')
        data = self.runner('cat', self.object_path(record))
        if (len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']
                or not data.startswith(b'age-encryption.org/v1\n')):
            raise core.FinixError('Rclone ciphertext readback mismatch')
        if output is not None:
            target = core.checked_path(output)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        return data

    def ledger(self):
        path = self.state / 'rclone.json'
        binding = self.binding()
        if not path.exists():
            journal = {'profile_id': self.owner, 'binding': binding, 'namespace': uuid.uuid4().hex, 'records': []}
            core.atomic_json(path, journal)
            return journal
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            journal = json.loads(core.read_regular(fd, 1024 * 1024))
        finally:
            os.close(fd)
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

    def backup_cipher(self, path, keep=0):
        if type(keep) is not int or not 0 <= keep <= MAX_RECORDS:
            raise core.FinixError('Invalid rclone retention')
        core.private_dir(self.state)
        with core.locked(self.state):
            fd = os.open(core.checked_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                data = core.read_regular(fd, MAX_CIPHER)
            finally:
                os.close(fd)
            if not data.startswith(b'age-encryption.org/v1\n'):
                raise core.FinixError('Only native age ciphertext may be uploaded')
            journal = self.ledger()
            if len(journal['records']) >= MAX_RECORDS:
                raise core.FinixError('Rclone ledger full; no upload')
            row = dict(profile_id=self.owner, remote=self.remote, root=self.root, type=journal['binding']['type'],
                       namespace=journal['namespace'], id=uuid.uuid4().hex + '.age',
                       size=len(data), sha256=hashlib.sha256(data).hexdigest())
            with tempfile.TemporaryDirectory(prefix='finix-rclone-', dir=os.environ.get('TMPDIR')) as stage:
                staged = Path(stage) / 'cipher.age'
                staged.write_bytes(data)
                staged.chmod(0o600)
                self.runner('copyto', str(staged), self.object_path(row), '--immutable')
            self.download(row)
            journal['records'].append(row)
            core.atomic_json(self.state / 'rclone.json', journal)
            if keep:
                self.prune(journal, keep)
            return dict(row, verified=True, storage='rclone')

    def prune(self, journal, keep):
        for row in list(journal['records'][:-keep]):
            try:
                self.download(row)
            except RcloneError as exc:
                if exc.code != 'NOT_FOUND':
                    raise
            else:
                self.runner('deletefile', self.object_path(row))
                try:
                    self.download(row)
                except RcloneError as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                else:
                    raise core.FinixError('Rclone deletion readback failed')
            journal['records'].remove(row)
            core.atomic_json(self.state / 'rclone.json', journal)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'download'])
    parser.add_argument('--home', required=True)
    parser.add_argument('--profile-id', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--remote', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--record')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    try:
        adapter = RcloneStorage(args.home, args.profile_id, Path(args.home) / 'unused-rclone-recovery',
                                args.config, args.remote, args.root)
        if args.command == 'status':
            result = adapter.status()
            print(json.dumps(result))
            return 0 if result['ready'] else 2
        if not args.record or not args.output:
            parser.error('download requires --record and --output')
        fd = os.open(core.checked_path(args.record), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            record = json.loads(core.read_regular(fd, 65536))
        finally:
            os.close(fd)
        adapter.download(record, args.output)
        print(json.dumps({'verified': True, 'sha256': record['sha256'], 'output': args.output}))
        return 0
    except (core.FinixError, OSError, ValueError, KeyError, TypeError):
        print('finix rclone: operation failed; provider diagnostics withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
