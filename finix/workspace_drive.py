"""Google Workspace storage using the owning profile's credential-owning helper.

No credential files are opened by Finix. Local readiness never contacts Google.
The private ledger, not remote names/listings, is the authority for retention.
"""
import hashlib
import importlib.util
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

from . import core

MAX_CIPHER = 129 * 1024 * 1024
MAX_RECORDS = 1000
MIME = 'application/octet-stream'
FOLDER_MIME = 'application/vnd.google-apps.folder'


class WorkspaceError(core.FinixError):
    def __init__(self, code):
        self.code = code
        super().__init__('Workspace ' + code + ': check the selected profile Workspace setup, Drive permissions and network; no login/fallback attempted')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise core.FinixError('Invalid Workspace object ID')
    return value


def helper_path(home):
    relative = Path('skills/productivity/google-workspace/scripts/google_api.py')
    own = core.checked_path(home) / relative
    if own.is_file():
        return core.checked_path(own)
    try:
        import hermes_constants
        builtin = Path(hermes_constants.__file__).parent / relative
        if builtin.is_file():
            return core.checked_path(builtin)
    except ImportError:
        pass
    return None


def child_env(home):
    # Never import/overlay the parent's secrets or dotenv. HOME is only used for
    # normal OS libraries; OAuth resolution is explicitly HERMES_HOME-bound.
    base = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
    base['HERMES_HOME'] = str(home)
    base['PYTHONNOUSERSITE'] = '1'
    return base


class WorkspaceRunner:
    def __init__(self, home, *, script=None, timeout=120, max_bytes=MAX_CIPHER):
        self.home = core.checked_path(home)
        self.script = core.checked_path(script) if script else helper_path(self.home)
        self.timeout, self.max_bytes = timeout, max_bytes

    def __call__(self, operation, *args):
        if not self.script:
            raise WorkspaceError('HELPER_MISSING')
        argv = [sys.executable, str(Path(__file__).with_name('workspace_helper.py')),
                str(self.script), str(self.max_bytes), operation, *map(str, args)]
        process = subprocess.Popen(argv, env=child_env(self.home), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        output = bytearray()
        total = 0
        deadline = time.monotonic() + self.timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise WorkspaceError('TIMEOUT')
                    for key, _ in selector.select(min(.1, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > 65536:
                            raise WorkspaceError('OUTPUT_LIMIT')
                        if key.data:
                            output.extend(chunk)
                process.wait(timeout=max(.01, deadline - time.monotonic()))
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError):
                raise WorkspaceError('INVALID_RESPONSE') from None
            if not isinstance(result, dict):
                raise WorkspaceError('INVALID_RESPONSE')
            if result.get('finix_error'):
                raise WorkspaceError(result['finix_error'] if result['finix_error'] in
                                     {'NOT_FOUND', 'PERMISSION'} else 'PROVIDER_ERROR')
            if process.returncode:
                raise WorkspaceError('PROVIDER_ERROR')
            return result
        except subprocess.TimeoutExpired:
            raise WorkspaceError('TIMEOUT') from None
        finally:
            # Kill only our isolated subprocess group, including any descendants.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


class WorkspaceDrive:
    def __init__(self, home, profile_id, state, *, runner=None):
        if not isinstance(profile_id, str) or not re.fullmatch(r'[a-f0-9]{64}', profile_id):
            raise core.FinixError('Invalid Finix profile binding')
        self.home, self.owner, self.state = core.checked_path(home), profile_id, core.checked_path(state)
        self.runner = runner or WorkspaceRunner(self.home)

    def status(self):
        script = helper_path(self.home)
        try:
            deps = all(importlib.util.find_spec(x) is not None for x in ('google.auth', 'googleapiclient'))
        except (ImportError, ValueError):
            deps = False
        # Presence only; readiness is not authorization or a live Drive probe.
        token = (self.home / 'google_token.json').is_file()
        code = 'HELPER_MISSING' if not script else 'DEPENDENCIES_MISSING' if not deps else 'TOKEN_ABSENT' if not token else 'LOCAL_READY'
        return {'ready': code == 'LOCAL_READY', 'code': code, 'live_google_verified': False,
                'action': 'Existing profile authorization must include Drive write access; Finix never starts OAuth.'}

    def metadata(self, obj):
        result = self.runner('metadata', identifier(obj))
        if result.get('id') != obj or type(result.get('trashed')) is not bool:
            raise core.FinixError('Invalid Workspace metadata')
        return result

    def folder_metadata(self, folder, owners=None):
        obj = self.metadata(folder)
        actual = obj.get('owners')
        if (obj.get('mimeType') != FOLDER_MIME or obj['trashed'] or not isinstance(actual, list)
                or not actual or any(not isinstance(x.get('emailAddress'), str) for x in actual)
                or (owners is not None and actual != owners)):
            raise core.FinixError('Workspace folder identity changed')
        return actual

    def descriptor(self, row):
        if (row.get('profile_id') != self.owner or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', ''))
                or type(row.get('size')) is not int or not 0 < row['size'] <= MAX_CIPHER):
            raise core.FinixError('Invalid Workspace recovery record')
        identifier(row.get('id'))
        identifier(row.get('folder_id'))
        return row

    def file_metadata(self, row):
        row = self.descriptor(row)
        obj = self.metadata(row['id'])
        if (obj['trashed'] or obj.get('mimeType') != MIME or obj.get('parents') != [row['folder_id']]
                or str(obj.get('size')) != str(row['size']) or obj.get('owners') != row.get('owners')):
            raise core.FinixError('Workspace ownership/metadata mismatch')
        return obj

    def download(self, record, output=None):
        record = self.descriptor(record)
        self.file_metadata(record)
        # The upstream CLI writes unbounded wb files: bound it in a private
        # encrypted-only stage with subprocess RLIMIT_FSIZE and timeout.
        with tempfile.TemporaryDirectory(prefix='finix-workspace-', dir=os.environ.get('TMPDIR')) as stage:
            path = Path(stage) / 'cipher.age'
            result = self.runner('download', record['id'], '--output', str(path))
            if result.get('status') != 'downloaded' or result.get('id') != record['id']:
                raise core.FinixError('Workspace download response mismatch')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                data = core.read_regular(fd, MAX_CIPHER)
            finally:
                os.close(fd)
        if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise core.FinixError('Workspace ciphertext readback mismatch')
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
        path = self.state / 'workspace.json'
        if not path.exists():
            # Never adopt by name after state loss: create a fresh namespace.
            result = self.runner('create-folder', 'Finix-' + self.owner[:12] + '-' + uuid.uuid4().hex)
            if result.get('status') != 'created':
                raise core.FinixError('Workspace folder creation failed')
            folder = identifier(result.get('id'))
            owners = self.folder_metadata(folder)
            journal = {'profile_id': self.owner, 'folder_id': folder, 'owners': owners, 'records': []}
            core.atomic_json(path, journal)
            return journal
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            journal = json.loads(core.read_regular(fd, 1024 * 1024))
        finally:
            os.close(fd)
        if journal.get('profile_id') != self.owner or not isinstance(journal.get('records'), list):
            raise core.FinixError('Workspace ledger binding mismatch')
        self.folder_metadata(identifier(journal.get('folder_id')), journal.get('owners'))
        ids = set()
        for row in journal['records']:
            self.descriptor(row)
            if (row['folder_id'] != journal['folder_id'] or row.get('owners') != journal['owners'] or row['id'] in ids):
                raise core.FinixError('Workspace ledger ownership mismatch')
            ids.add(row['id'])
        return journal

    def backup_cipher(self, path, keep=0):
        if type(keep) is not int or not 0 <= keep <= MAX_RECORDS:
            raise core.FinixError('Invalid Workspace retention')
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
                raise core.FinixError('Workspace ledger full; no upload')
            # Stable private staging prevents source replacement during upload.
            with tempfile.TemporaryDirectory(prefix='finix-upload-', dir=os.environ.get('TMPDIR')) as stage:
                staged = Path(stage) / 'cipher.age'
                staged.write_bytes(data)
                staged.chmod(0o600)
                result = self.runner('upload', str(staged), '--name', 'finix-' + uuid.uuid4().hex + '.age',
                                     '--parent', journal['folder_id'], '--mime-type', MIME)
            if result.get('status') != 'uploaded' or result.get('mimeType') != MIME:
                raise core.FinixError('Workspace upload response mismatch')
            row = dict(id=identifier(result.get('id')), profile_id=self.owner, folder_id=journal['folder_id'],
                       owners=journal['owners'], sha256=hashlib.sha256(data).hexdigest(), size=len(data))
            if row['id'] in {x['id'] for x in journal['records']}:
                raise core.FinixError('Workspace returned an existing object ID')
            self.download(row)
            journal['records'].append(row)
            core.atomic_json(self.state / 'workspace.json', journal)
            if keep:
                self.prune(journal, keep)
            return dict(row, verified=True, storage='workspace')

    def prune(self, journal, keep):
        for old in list(journal['records'][:-keep]):
            try:
                obj = self.metadata(old['id'])
            except WorkspaceError as exc:
                if exc.code != 'NOT_FOUND':
                    raise
                obj = None
            if obj is not None and not obj['trashed']:
                self.download(old)  # fresh ownership, parent, size, and SHA readback
                result = self.runner('delete', old['id'])  # reversible trash only
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
        drive = WorkspaceDrive(args.home, args.profile_id, Path(args.home) / 'unused-finix-recovery-state')
        if args.command == 'status':
            result = drive.status()
            print(json.dumps(result))
            return 0 if result['ready'] else 2
        if not args.record or not args.output:
            parser.error('download requires --record and --output')
        fd = os.open(core.checked_path(args.record), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            record = json.loads(core.read_regular(fd, 65536))
        finally:
            os.close(fd)
        drive.download(record, args.output)
        print(json.dumps({'verified': True, 'sha256': record['sha256'], 'output': args.output}))
        return 0
    except (core.FinixError, OSError, ValueError, KeyError, TypeError):
        print('finix workspace: operation failed; provider diagnostics withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
