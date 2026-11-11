"""Profile-bound orchestration. No secret keys or auth state are read here."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import time

from . import core


def _home_binding(home):
    return hashlib.sha256(str(home).encode()).hexdigest()


def _windows_binding_anchors(home):
    """Retain trusted home/routing ancestry until routing publication completes."""
    from contextlib import ExitStack, contextmanager
    from .windows_native import canonical_directory, directory, trusted_handle

    @contextmanager
    def retained():
        value = os.fspath(home)
        if value.startswith('\\\\?\\'):
            value = value[4:]
        with ExitStack() as stack:
            pinned = stack.enter_context(directory(value))
            for handle in pinned.handles[:-1]:
                if not trusted_handle(handle, ancestor=True):
                    raise core.FinixError('PROFILE_BINDING_UNTRUSTED')
            if not trusted_handle(pinned.handle, private=True):
                raise core.FinixError('PROFILE_BINDING_UNTRUSTED')
            # Resolve metadata while the original traversal is still retained.
            # canonical_directory also rejects case-sensitive ancestor trees.
            canonical, identity = canonical_directory(value)
            if pinned.identity() != identity:
                raise core.FinixError('PROFILE_BINDING_CHANGED')
            parent = canonical.parent / 'finix-data'
            core.private_dir(parent)
            routing = stack.enter_context(directory(parent))
            for handle in routing.handles[:-1]:
                if not trusted_handle(handle, ancestor=True):
                    raise core.FinixError('PROFILE_BINDING_UNTRUSTED')
            if not trusted_handle(routing.handle, private=True):
                raise core.FinixError('PROFILE_BINDING_UNTRUSTED')
            _, routing_identity = canonical_directory(parent)
            if routing.identity() != routing_identity:
                raise core.FinixError('PROFILE_BINDING_CHANGED')
            yield canonical, identity, parent
    return retained()


def _windows_profile_binding(home):
    with _windows_binding_anchors(home) as (canonical, identity, parent):
        return _windows_profile_binding_selected(home, canonical, identity, parent)


def _windows_binding_populated(root, retained):
    """Ignore only the exact inactive status-GET scaffold, never job state.

    Keep the validated namespace, status directory and byte-range lease pinned
    through routing publication. Do not create a lease or write in this alias.
    An empty backup-jobs directory is conservatively populated: no existing
    producer lease can fence its first writer without mutating that namespace.
    """
    import errno
    import msvcrt
    from .windows_native import directory, information, trusted_handle

    anchor = retained.enter_context(directory(root))
    if not trusted_handle(anchor.handle, private=True):
        raise core.FinixError('PROFILE_BINDING_UNTRUSTED')
    names = {p.name for p in root.iterdir()}
    if not names - {'backend.json'}:
        return False
    if names != {'backend.json', 'backup-jobs'}:
        return True
    # Fence ordinary legacy settings/backup writers as well as job writers.
    # Existing native mutex only: no file is added to the ignored namespace.
    retained.enter_context(core.locked(root))
    jobs = root / 'backup-jobs'
    anchor = retained.enter_context(directory(jobs))
    if not trusted_handle(anchor.handle, private=True):
        raise core.FinixError('PROFILE_BINDING_UNTRUSTED')
    if {p.name for p in jobs.iterdir()} != {'state.lock'}:
        return True
    fd = retained.enter_context(core.regular_input(jobs / 'state.lock'))
    if not trusted_handle(msvcrt.get_osfhandle(fd), private=True):
        raise core.FinixError('PROFILE_BINDING_UNTRUSTED')
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return True
        raise
    def unlock():
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    retained.callback(unlock)
    if information(msvcrt.get_osfhandle(fd)).links != 1:
        raise core.FinixError('PROFILE_BINDING_UNTRUSTED')
    if core.read_regular(fd, 2) != b'0':
        return True
    # Inspect again under the real producer's lease, closing admission races.
    return ({p.name for p in jobs.iterdir()} != {'state.lock'}
            or {p.name for p in root.iterdir()} != names)


def _windows_profile_binding_selected(home, canonical, identity, parent):
    """Select, never transplant, an owned legacy namespace for this directory.

    Only bounded known spellings are inspected. The durable record retains the
    exact source spelling: transport namespaces and pending intents depend on it.
    """
    from contextlib import ExitStack
    from .windows_native import canonical_directory, directory
    provided = os.fspath(home)
    if provided.startswith('\\\\?\\'):
        provided = provided[4:]
    provided = core.checked_path(provided)
    record_path = parent / ('home-binding-' + _home_binding(canonical) + '.json')
    with directory(canonical) as pinned, core.locked(parent), ExitStack() as retained:
        if pinned.identity() != identity:
            raise core.FinixError('PROFILE_BINDING_CHANGED')
        record = None
        if record_path.exists():
            with core.regular_input(record_path) as fd:
                record = json.loads(core.read_regular(fd, 65536))
            if (not isinstance(record, dict) or record.get('version') != 1
                    or record.get('identity') != list(identity)
                    or not isinstance(record.get('home'), str)
                    or record.get('binding') != _home_binding(record['home'])):
                raise core.FinixError('PROFILE_BINDING_CONFLICT')
        spellings = [str(provided), str(canonical), str(canonical).lower()]
        # Native discovery commonly changes only drive-letter case. Never use
        # case folding as identity: every candidate must reopen this same inode.
        spellings += [s[0].swapcase() + s[1:] for s in tuple(spellings)]
        env_home = os.environ.get('HERMES_HOME')
        if env_home:
            spellings.append(env_home)
        if record:
            spellings.append(record['home'])
        candidates = {}
        for spelling in dict.fromkeys(spellings):
            candidate = core.checked_path(spelling)
            try:
                _, candidate_id = canonical_directory(candidate)
            except (OSError, ValueError):
                if record and spelling == record['home']:
                    raise core.FinixError('PROFILE_BINDING_CONFLICT') from None
                continue
            if candidate_id != identity:
                if record and spelling == record['home']:
                    raise core.FinixError('PROFILE_BINDING_CONFLICT')
                continue
            binding = _home_binding(candidate)
            root = parent / binding
            populated = False
            if root.exists():
                # Validate ownership/ACL/no-reparse before enumerating this one
                # known binding. No settings, auth or private key contents read.
                core.private_dir(root)
                populated = _windows_binding_populated(root, retained)
            candidates[binding] = (candidate, populated)
        populated = [key for key, (_, state) in candidates.items() if state]
        if len(populated) > 1:
            raise core.FinixError('PROFILE_BINDING_CONFLICT')
        if record:
            binding = record['binding']
            if binding not in candidates or (populated and populated != [binding]):
                raise core.FinixError('PROFILE_BINDING_CONFLICT')
            selected = candidates[binding][0]
        elif populated:
            binding = populated[0]
            selected = candidates[binding][0]
        else:
            existing = [key for key in candidates if (parent / key / 'backend.json').is_file()]
            if len(existing) > 1:
                raise core.FinixError('PROFILE_BINDING_CONFLICT')
            if existing:
                binding = existing[0]
                selected = candidates[binding][0]
            else:
                binding, selected = _home_binding(canonical), canonical
        selected_root = selected.parent / 'finix-data' / binding
        identity_path = selected_root / 'backend.json'
        if record and not identity_path.is_file():
            raise core.FinixError('PROFILE_BINDING_CONFLICT')
        if not record:
            core.private_dir(selected_root)
            if not identity_path.exists():
                with core.locked(selected_root):
                    if not identity_path.exists():
                        import secrets
                        core.atomic_json(identity_path, {'profile_id': secrets.token_hex(32)})
            with core.regular_input(identity_path) as fd:
                backend_id = json.loads(core.read_regular(fd, 1024))['profile_id']
            core.atomic_json(record_path, {'version': 1, 'identity': list(identity),
                'home': str(selected), 'binding': binding, 'backend_id': backend_id})
        else:
            with core.regular_input(identity_path) as fd:
                backend_id = json.loads(core.read_regular(fd, 1024))['profile_id']
            if record.get('backend_id') != backend_id:
                raise core.FinixError('PROFILE_BINDING_CONFLICT')
        return selected, selected_root


class Profile:
    def __init__(self, home, *, default_root=None):
        if os.name == 'nt':
            self.home, self.root = _windows_profile_binding(home)
        else:
            self.home = core.checked_path(home)
            self.root = self.home.parent / 'finix-data' / _home_binding(self.home)
        from .extra_sources import trusted_root
        self.default_root = default_root if default_root is not None else trusted_root()
        # Random backend identity prevents same-path profiles on different servers
        # accepting stale recipient POSTs. It is not part of the restored profile.
        core.private_dir(self.root)
        identity = self.root / 'backend.json'
        # Immutable backend identity already exists for every admitted restore.
        # Do not contend on the long-lived backup/restore operation lock merely
        # to resolve an authenticated polling/cancel request's owning profile.
        if not identity.exists():
            with core.locked(self.root):
                if not identity.exists():
                    import secrets
                    core.atomic_json(identity, {'profile_id': secrets.token_hex(32)})
        with core.regular_input(identity) as fd:
            self.profile_id = json.loads(core.read_regular(fd, 1024))['profile_id']

    def directories(self):
        core.private_dir(self.root)
        return self.root

    def config(self):
        path = self.root / 'settings.json'
        if not path.exists():
            return {'recipient': None, 'storage': 'local', 'retention': 0, 'interval_hours': 0,
                    'next_run': 0, 'folder_id': None}
        with core.regular_input(path) as fd:
            return json.loads(core.read_regular(fd, 65536))

    def connector(self, cfg=None):
        from .connector_drive import ManagedDrive
        cfg = cfg or self.config()
        return ManagedDrive(self.home, self.profile_id, cfg.get('connector_account', ''),
                            cfg.get('connector_alias', ''), cfg.get('connector_folder', ''))

    def workspace(self):
        from .workspace_drive import WorkspaceDrive
        return WorkspaceDrive(self.home, self.profile_id, self.root / 'workspace')

    def rclone(self, cfg=None):
        from .rclone_storage import RcloneStorage
        cfg = cfg or self.config()
        required = ('rclone_config', 'rclone_remote', 'rclone_root')
        if any(not cfg.get(key) for key in required):
            from .rclone_storage import RcloneError
            raise RcloneError('SELECTION_REQUIRED')
        state = self.root / 'rclone'
        if cfg.get('rclone_connection'):
            import re
            if not re.fullmatch(r'[a-f0-9]{32}', cfg['rclone_connection']):
                raise core.FinixError('Invalid connection binding')
            state = state / cfg['rclone_connection']
        return RcloneStorage(self.home, self.profile_id, state,
                             cfg['rclone_config'], cfg['rclone_remote'], cfg['rclone_root'])

    def capture_status(self):
        try:
            with core.regular_input(self.root / 'capture-status.json') as fd:
                value = json.loads(core.read_regular(fd, 1024))
        except FileNotFoundError:
            return None
        if isinstance(value, dict) and value.get('complete', 'missing') is None and value.get('omitted_count', 'missing') is None:
            return {'complete': None, 'omitted_count': None}
        if (not isinstance(value, dict) or type(value.get('complete')) is not bool
                or type(value.get('omitted_count')) is not int or value['omitted_count'] < 0
                or value['complete'] != (value['omitted_count'] == 0)):
            raise core.FinixError('Invalid capture status')
        return {key: value[key] for key in ('complete', 'omitted_count')}

    def status(self):
        cfg = self.config()
        result = {'profile_id': self.profile_id, **cfg,
                  'scheduler': 'automatic while native plugin is loaded; optional python -m finix.scheduler when host is offline'}
        from .extra_sources import records, read_config
        result['extra_sources'] = records(read_config(self.home)[0])
        result['extra_sources_allowed'] = self.home == self.default_root
        result['capture'] = self.capture_status()
        result['sqlite_temp'] = core.sqlite_temp_readiness(cfg.get('sqlite_temp_dir'))
        if cfg['storage'] == 'connector-drive':
            from .connector_drive import ConnectorError, ACTIONS
            try:
                result['managed_connector'] = self.connector(cfg).status()
            except ConnectorError as exc:
                result['managed_connector'] = {'ready': False, 'code': exc.code, 'action': ACTIONS[exc.code]}
        if cfg['storage'] == 'workspace':
            result['workspace'] = self.workspace().status()
        if cfg['storage'] == 'rclone':
            from .rclone_storage import RcloneError
            try:
                result['rclone'] = self.rclone(cfg).status()
            except RcloneError as exc:
                result['rclone'] = {'ready': False, 'code': exc.code, 'action': str(exc)}
        return result

    def recipient(self, profile_id, recipient, expected):
        if profile_id != self.profile_id:
            raise core.FinixError('Profile changed; reload status')
        if not isinstance(recipient, str) or not core.RECIPIENT.fullmatch(recipient):
            raise core.FinixError('Expected one native age public recipient')
        with core.locked(self.directories()):
            cfg = self.config()
            if cfg['recipient'] is not None and cfg['recipient'] != recipient:
                raise core.FinixError('Registered public recipient is immutable; replacement is not permitted')
            if cfg['recipient'] != expected:
                raise core.FinixError('Recipient changed; reload and confirm the current recipient')
            cfg['recipient'] = recipient
            core.atomic_json(self.root / 'settings.json', cfg)
        return self.status()

    def configure(self, key, value):
        if key == 'interval_hours' and int(value) > 0:
            from .setup_flow import SetupFlow
            SetupFlow(self).schedule(str(int(value)) + 'h')
            return self.status()
        with core.locked(self.directories()):
            cfg = self.config()
            if key in {'retention', 'interval_hours'}:
                value = int(value)
                if not 0 <= value <= (1000 if key == 'retention' else 8760):
                    raise core.FinixError('Configuration value out of range')
                if key == 'interval_hours':
                    cfg['next_run'] = time.time() + value * 3600 if value else 0
            elif key == 'storage':
                if value not in {'local', 'drive', 'connector-drive', 'workspace', 'rclone'}:
                    raise core.FinixError('Storage must be local, drive, workspace, rclone or connector-drive')
            elif key == 'sqlite_temp_dir':
                import stat
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise core.FinixError('SQLite temporary storage requires an absolute directory')
                path = core.checked_path(value)
                info = path.stat()
                if os.name == 'nt':
                    from .windows_native import trusted_directory
                    private = trusted_directory(path, private=True)
                else:
                    private = (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                               and stat.S_IMODE(info.st_mode) == 0o700)
                if not private:
                    raise core.FinixError('NEED_SQLITE_TEMP_DIRECTORY')
                from .extra_sources import load
                extras = load(self.home, self.default_root, (self.root.parent,))
                core.validate_sqlite_temp_location(path, self.home, self.root,
                    *(Path(item['path']) for item in extras))
                value = str(path)
            elif key in {'rclone_config', 'rclone_remote', 'rclone_root'}:
                from .rclone_storage import config_path, remote_name, relative_root
                validator = {'rclone_config': config_path, 'rclone_remote': remote_name, 'rclone_root': relative_root}[key]
                value = str(validator(value))
                cfg.pop('rclone_connection', None)
                if key == 'rclone_config' and Path(value).is_relative_to(self.home):
                    raise core.FinixError('Rclone credentials must remain outside the backed-up profile')
            elif key in {'connector_account', 'connector_alias', 'connector_folder'}:
                import re
                if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,200}', value):
                    raise core.FinixError('Expected an explicit managed account ID, alias or folder ID')
            else:
                raise core.FinixError('Allowed settings: storage, retention, interval_hours, sqlite_temp_dir, connector_account, connector_alias, connector_folder, rclone_config, rclone_remote, rclone_root')
            cfg[key] = value
            core.atomic_json(self.root / 'settings.json', cfg)
        return self.status()

    def extra_sources(self, action, name=None, path=None):
        from .extra_sources import edit
        return edit(self, action, name, path)

    def backup(self, scheduled=False, *, progress=None, cancel=None, resume=False):
        with core.locked(self.directories()):
            cfg = self.config()
            now = time.time()
            if scheduled and (not cfg['interval_hours'] or cfg['next_run'] > now):
                return {'skipped': True}
            if scheduled:
                from .setup_flow import SetupFlow
                if not SetupFlow(self).view()['verified']:
                    cfg.update(interval_hours=0, next_run=0)
                    core.atomic_json(self.root / 'settings.json', cfg)
                    raise core.FinixError('Setup changed; scheduling paused until a successful setup check')
            if not cfg['recipient']:
                raise core.FinixError('Register a public recipient first')
            from .streaming import check_cancel
            check_cancel(cancel)
            if resume:
                return self._resume_backup(cfg, resume, progress=progress, cancel=cancel)
            transport_checkpoint = {}
            if cfg['storage'] == 'rclone':
                adapter = self.rclone(cfg)
                adapter.require_no_pending()
                from .rclone_storage import RcloneStorage
                if isinstance(adapter, RcloneStorage):
                    with adapter._locked():
                        transport_checkpoint['namespace'] = adapter.ledger()['namespace']
            # Do not prune local copies until cloud readback has succeeded.
            result = core.backup(self.home, self.root / 'copies', self.root / 'index', cfg['recipient'],
                                 sqlite_temp_dir=cfg.get('sqlite_temp_dir'),
                                 configured_extras=True, default_root=self.default_root,
                                 extra_forbidden=(self.root.parent,), progress=progress, cancel=cancel)
            capture = {key: result[key] for key in ('complete', 'omitted_count')}
            core.atomic_json(self.root / 'capture-status.json', capture)
            if progress is not None:
                progress({'phase': 'captured', 'archive': Path(result['path']).name,
                          'sha256': result['sha256'], 'size': Path(result['path']).stat().st_size,
                          **transport_checkpoint, **capture})
            keep = cfg['retention'] if result['complete'] else 0
            if not result['complete']:
                (self.root / 'setup-verification.json').unlink(missing_ok=True)
            transport_options = {}
            if progress is not None:
                transport_options['progress'] = progress
                progress({'phase': 'upload'})
            if cancel is not None: transport_options['cancel'] = cancel
            if cfg['storage'] == 'drive':
                from .drive import Drive
                drive = Drive(self.profile_id)
                if not cfg['folder_id']:
                    cfg['folder_id'] = drive.create_folder()
                    core.atomic_json(self.root / 'settings.json', cfg)
                result['drive'] = drive.upload(Path(result['path']), cfg['folder_id'], keep, completion=result)
            elif cfg['storage'] == 'workspace':
                result['workspace'] = self.workspace().backup_cipher(
                    Path(result['path']), keep, completion=result, **transport_options)
                check_cancel(cancel)
                if (result['workspace'].get('verified') is not True
                        or result['workspace'].get('sha256') != result['sha256']):
                    raise core.FinixError('PENDING_RECONCILIATION')
                # Public recovery record, independent of the original server ledger.
                core.atomic_json(Path(result['path'] + '.workspace.json'), result['workspace'])
                check_cancel(cancel)
            elif cfg['storage'] == 'connector-drive':
                result['managed_connector'] = self.connector(cfg).upload(
                    Path(result['path']), self.root / 'connector-index', keep, completion=result)
            elif cfg['storage'] == 'rclone':
                result['rclone'] = self.rclone(cfg).backup_cipher(Path(result['path']), keep, completion=result, **transport_options)
                core.atomic_json(Path(result['path'] + '.rclone.json'), result['rclone'])
            if keep:
                self._prune_local(keep)
            cfg['next_run'] = now + cfg['interval_hours'] * 3600 if cfg['interval_hours'] else 0
            core.atomic_json(self.root / 'settings.json', cfg)
            return result

    def pending_backup_checkpoint(self, *, progress=None, cancel=None):
        """Explicit Start only: resolve the one owned pre-jobs pending cipher.

        No caller paths, account scans, capture, upload or inferred completeness.
        The caller durably saves this capability before invoking resume.
        """
        with core.locked(self.directories()):
            cfg = self.config()
            if cfg['storage'] != 'rclone':
                return None
            adapter = self.rclone(cfg)
            from .rclone_storage import RcloneStorage
            if not isinstance(adapter, RcloneStorage):
                return None
            with adapter._locked():
                if adapter._read('rclone.json', 1024 * 1024) is None:
                    if adapter._read('rclone-pending.json', 65536) is not None:
                        raise core.FinixError('PENDING_RECONCILIATION')
                    return None
                journal = adapter.ledger(cancel=cancel)
                pending = adapter.pending(journal)
                if pending is None:
                    return None
                conflicts = [r for r in journal['records'] if r['id'] == pending['id']]
                if conflicts and conflicts != [pending]:
                    raise core.FinixError('PENDING_RECONCILIATION')
                return self._owned_pending_checkpoint(pending, progress=progress, cancel=cancel)

    def _owned_pending_checkpoint(self, pending, *, progress=None, cancel=None):
        from . import cloud_files
        import stat
        # Directory leases pin the private no-follow copies/index ancestors.
        copies = core.checked_path(self.root / 'copies')
        index = core.checked_path(self.root / 'index')
        with core.locked(copies), core.locked(index):
            for directory in (copies, index):
                if os.name == 'nt':
                    from .windows_native import trusted_directory
                    safe = trusted_directory(directory, private=True)
                else:
                    info = directory.stat()
                    safe = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
                if not safe:
                    raise core.FinixError('PENDING_RECONCILIATION')
            with core.regular_input(index / 'index.json') as fd:
                self._require_private_retained_file(fd)
                state = core.load_state(index)
            rows = [r for r in (state or {}).get('backups', []) if r['sha256'] == pending['sha256']]
            if len(rows) != 1:
                raise core.FinixError('PENDING_RECONCILIATION')
            row = rows[0]
            path = copies / row['name']
            with core.regular_input(path) as fd:
                self._require_private_retained_file(fd)
                actual = cloud_files.describe(path, native=True, progress=progress, cancel=cancel, phase='checksum')
            if any(actual[k] != pending[k] for k in ('sha256', 'size')):
                raise core.FinixError('PENDING_RECONCILIATION')
            # Older index formats may carry a bool but no omission census.
            # That is not the current digest-bound completeness contract.
            local = cloud_files.completion_record(row, row if all(k in row for k in ('complete', 'omitted_count')) else None)
            remote = cloud_files.completion_record(pending, pending.get('completion'))
            if local is not None and remote is not None and local != remote:
                raise core.FinixError('PENDING_RECONCILIATION')
            # Missing historical metadata is uncertainty, not zero omissions.
            provenance = local if local is not None and remote is not None else None
            return dict(archive=row['name'], **actual, namespace=pending['namespace'],
                        complete=provenance['complete'] if provenance else None,
                        omitted_count=provenance['omitted_count'] if provenance else None,
                        legacy_pending=pending, local_namespace=state['namespace'],
                        local_record_sha256=hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest())

    @staticmethod
    def _require_private_retained_file(fd):
        import stat
        info = os.fstat(fd)
        if os.name == 'nt':
            import msvcrt
            from .windows_native import trusted_handle
            safe = trusted_handle(msvcrt.get_osfhandle(fd), private=True)
        else:
            safe = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
        if not safe or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise core.FinixError('PENDING_RECONCILIATION')

    def _resume_backup(self, cfg, intent, *, progress=None, cancel=None):
        """Reconcile the exact retained ciphertext; never take a new snapshot."""
        if not isinstance(intent, dict):
            raise core.FinixError('PENDING_RECONCILIATION')
        if cfg['storage'] == 'workspace':
            from . import cloud_files
            name = intent.get('archive')
            if (not isinstance(name, str) or Path(name).name != name
                    or '/' in name or '\\' in name):
                raise core.FinixError('PENDING_RECONCILIATION')
            copies, index = self.root / 'copies', self.root / 'index'
            # Validate local authority before opening a provider adapter. Never
            # infer remote absence from a missing local ledger or pending file.
            with core.locked(copies), core.locked(index):
                with core.regular_input(index / 'index.json') as fd:
                    self._require_private_retained_file(fd)
                    state = core.load_state(index)
                if (state.get('source') != hashlib.sha256(str(self.home).encode()).hexdigest()
                        or state.get('output') != str(copies)):
                    raise core.FinixError('PENDING_RECONCILIATION')
                rows = [row for row in state['backups'] if row.get('name') == name
                        and row.get('sha256') == intent.get('sha256')]
                if len(rows) != 1:
                    raise core.FinixError('PENDING_RECONCILIATION')
                path = copies / name
                with core.regular_input(path) as fd:
                    self._require_private_retained_file(fd)
                    actual = cloud_files.describe(path, native=True, progress=progress,
                                                  cancel=cancel, phase='checksum')
                if (type(intent.get('size')) is not int
                        or any(actual[key] != intent.get(key) for key in ('sha256', 'size'))):
                    raise core.FinixError('PENDING_RECONCILIATION')
                row = rows[0]
                provenance = (cloud_files.completion_record(row, row)
                              if all(key in row for key in ('complete', 'omitted_count'))
                              and not (row['complete'] is None and row['omitted_count'] is None) else None)
                expected = {key: provenance[key] if provenance else None
                            for key in ('complete', 'omitted_count')}
                if (any(intent.get(key) != value for key, value in expected.items())
                        or provenance and any(type(intent.get(key)) is not type(value)
                                              for key, value in expected.items())):
                    raise core.FinixError('PENDING_RECONCILIATION')
                adapter = self.workspace()
                # Older adapters create a fresh object on every backup_cipher.
                # Fail closed rather than turning continuation into a duplicate.
                if not callable(getattr(adapter, 'reconcile', None)):
                    raise core.FinixError('PENDING_RECONCILIATION')
                remote = adapter.reconcile(path, completion=intent if provenance else None,
                                           progress=progress, cancel=cancel)
                from .streaming import check_cancel
                check_cancel(cancel)
                if remote.get('verified') is not True or remote.get('sha256') != actual['sha256']:
                    raise core.FinixError('PENDING_RECONCILIATION')
                result = dict(path=str(path), sha256=actual['sha256'], workspace=remote, **expected)
                check_cancel(cancel)
                core.atomic_json(Path(str(path) + '.workspace.json'), remote)
                check_cancel(cancel)
                core.atomic_json(self.root / 'capture-status.json', expected)
                return result
        if cfg['storage'] != 'rclone':
            raise core.FinixError('PENDING_RECONCILIATION')
        adapter = self.rclone(cfg)
        with adapter._locked():
            return self._resume_backup_locked(cfg, intent, adapter, progress=progress, cancel=cancel)

    def _resume_backup_locked(self, cfg, intent, adapter, *, progress=None, cancel=None):
        # Keep adapter ownership from checkpoint comparison through readback and
        # commit; a direct administrative reconciliation cannot switch intent.
        name = intent.get('archive')
        if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name:
            raise core.FinixError('Invalid retained archive identity')
        path = self.root / 'copies' / name
        if core.digest_file(path, cancel=cancel, progress=progress) != intent.get('sha256'):
            raise core.FinixError('Retained ciphertext changed')
        transport_options = {}
        if progress is not None: transport_options['progress'] = progress
        if cancel is not None: transport_options['cancel'] = cancel
        with adapter._locked():
            if hasattr(adapter, '_read') and adapter._read('rclone.json', 1024 * 1024) is None:
                raise core.FinixError('PENDING_RECONCILIATION')
            journal = adapter.ledger()
            pending = adapter.pending(journal)
            if 'legacy_pending' in intent:
                original = intent['legacy_pending']
                checkpoint = self._owned_pending_checkpoint(original, progress=progress, cancel=cancel)
                if checkpoint != intent or intent['namespace'] != journal['namespace']:
                    raise core.FinixError('PENDING_RECONCILIATION')
                committed = [r for r in journal['records'] if r['id'] == original['id']]
                if (pending is not None and pending != original
                        or committed and committed != [original]
                        or pending is None and committed != [original]):
                    raise core.FinixError('PENDING_RECONCILIATION')
        if pending:
            if pending['sha256'] != intent['sha256']:
                raise core.FinixError('Pending ciphertext identity changed')
            remote = adapter.reconcile(path, **transport_options)
        else:
            rows = [row for row in journal['records'] if row['sha256'] == intent['sha256']]
            if len(rows) > 1:
                raise core.FinixError('PENDING_RECONCILIATION')
            if rows:
                adapter.download(rows[0], **transport_options)
                remote = dict(rows[0], verified=True)
            else:
                # Intent is fsynced before any provider copy and cleared only
                # after the committed ledger. Thus this exact owned checkpoint
                # with an intact ledger and no intent/row is a pre-intent gap,
                # never permission to abandon an unknown pending object.
                if (not hasattr(adapter, '_read') or intent.get('namespace') != journal['namespace']):
                    raise core.FinixError('PENDING_RECONCILIATION')
                owned = [r for r in core.load_state(self.root / 'index')['backups']
                         if r.get('name') == name and r.get('sha256') == intent['sha256']]
                if (len(owned) != 1 or type(intent.get('size')) is not int
                        or path.stat().st_size != intent['size']
                        or any(owned[0].get(k) != intent[k] for k in ('complete', 'omitted_count'))):
                    raise core.FinixError('PENDING_RECONCILIATION')
                remote = adapter.backup_cipher(path, 0, completion=intent, **transport_options)
        result = dict(path=str(path), sha256=intent['sha256'], complete=intent['complete'],
                      omitted_count=intent['omitted_count'], rclone=remote)
        core.atomic_json(self.root / 'capture-status.json',
                         {key: result[key] for key in ('complete', 'omitted_count')})
        return result

    def _prune_local(self, keep):
        state, output = self.root / 'index', self.root / 'copies'
        with core.locked(state), core.locked(output):
            index = core.load_state(state)
            capture = self.capture_status()
            if capture and capture['complete'] is not True:
                return
            if index['backups'] and (index['backups'][-1].get('complete') is not True
                                     or index['backups'][-1].get('omitted_count') != 0):
                return  # A verified ciphertext is not a complete retention successor.
            old = []
            for row in index['backups'][:-keep]:
                if type(row.get('complete')) is not bool:
                    continue
                # Explicit connection transitions retain failed owned captures
                # at their original address, outside the new account's retention.
                pin = self.root / 'backup-jobs' / 'retained-captures' / (row['name'] + '.json')
                try:
                    with core.regular_input(pin) as fd:
                        retained = json.loads(core.read_regular(fd, 16384))
                except FileNotFoundError:
                    old.append(row)
                    continue
                if retained.get('archive') != row['name'] or retained.get('sha256') != row['sha256']:
                    raise core.FinixError('Retention capture binding changed')
            for record in old:
                try:
                    actual = core.digest_file(output / record['name'])
                except FileNotFoundError:
                    # A previous run may have deleted the file before saving its index.
                    continue
                if actual != record['sha256']:
                    raise core.FinixError('Retention candidate changed')
            for record in old:
                (output / record['name']).unlink(missing_ok=True)
                index['backups'].remove(record)
                # Persist progress so one transient delete failure cannot wedge scheduling.
                core.atomic_json(state / 'index.json', index)


def current_profile():
    from hermes_constants import get_hermes_home
    return Profile(get_hermes_home())


def command(raw_args, *, owner_id='', owner_platform='', platform='', source=None, connection_commands=None, bound_profile=None, **kwargs):
    """Only adapter-attested identities may use chat operations, including status."""
    if (not owner_id or not owner_platform or source is None or platform != owner_platform
            or str(source.user_id) != str(owner_id)):
        return 'Finix denied: configure owner_id and owner_platform locally; trusted owner context required.'
    try:
        args = shlex.split(raw_args)
        profile = bound_profile() if bound_profile else current_profile()
        if not args or args == ['status']:
            return json.dumps(profile.status())
        if args and args[0] == 'connect':
            if getattr(source, 'chat_type', '') != 'dm':
                return 'Finix connect: PRIVATE_CHAT_REQUIRED; подключайте облако только в личном чате владельца.'
            from .connect_commands import default_commands
            from .connections import ConnectionError
            try:
                return (connection_commands or default_commands).execute(
                    profile, args[1:], (platform, str(source.user_id)))
            except ConnectionError as exc:
                return 'Finix connect: ' + exc.code
        if args == ['setup']:
            return ('Generate and save your private key only on your device: open the shipped '
                    'dist/keygen.html offline (authenticated backend download: '
                    '/api/plugins/finix/keygen.html). Send only the public age1 recipient. '
                    'Use /finix recipient NEW none for initial registration; '
                    'an existing public recipient cannot be replaced. '
                    'Storage choices: local; drive (own OAuth: hermes auth add finix:google in owner terminal); '
                    'connector-drive (experimental/unavailable, actual gateway cap 20 MiB; not for large archives: explicitly select connector_account, '
                    'connector_alias, connector_folder; no automatic login); '
                    'workspace (existing owning-profile Google Workspace authorization; no token copy or login); '
                    'rclone (existing Yandex Disk, OneDrive Personal or Dropbox remote; explicit rclone_config absolute path, '
                    'rclone_remote name and rclone_root relative folder; config outside the backed-up profile). '
                    'New account connection: /finix connect shows the owner-only connection commands.')
        if args[0] == 'recipient' and len(args) in {3, 4}:
            expected = None if args[2] == 'none' else args[2]
            if expected is not None and args[3:] != ['confirm']:
                raise core.FinixError('Replacement requires OLD recipient and confirm')
            return json.dumps(profile.recipient(profile.profile_id, args[1], expected))
        if args[0] == 'extra':
            if args[1:] == ['list']:
                return json.dumps(profile.extra_sources('list'))
            if len(args) == 5 and args[1] == 'add':
                raise core.FinixError('Quote paths with spaces')
            if len(args) == 4 and args[1] == 'add':
                return json.dumps(profile.extra_sources('add', args[2], args[3]))
            if len(args) == 3 and args[1] == 'remove':
                return json.dumps(profile.extra_sources('remove', args[2]))
            return 'Usage: /finix extra list|add NAME ABSOLUTE_PATH|remove NAME'
        if args == ['backup']:
            return json.dumps(profile.backup())
        if args[0] == 'configure' and len(args) == 3:
            return json.dumps(profile.configure(args[1], args[2]))
        if args == ['restore']:
            return 'Restore only on your recovery device: python -m finix restore --archive FILE --identity KEYFILE --target NEW_DIRECTORY. Never send a private key to chat.'
        return 'Usage: /finix status|setup|recipient NEW none|backup|configure storage local/drive/workspace/rclone/connector-drive|configure sqlite_temp_dir ABSOLUTE_PRIVATE_DIRECTORY|configure rclone_config ABSOLUTE_PATH|configure rclone_remote NAME|configure rclone_root RELATIVE_FOLDER|configure connector_account ID|configure connector_alias ALIAS|configure connector_folder ID|configure retention N|configure interval_hours N|restore'
    except core.FinixError as exc:
        from .connector_drive import ConnectorError
        from .workspace_drive import WorkspaceError
        from .rclone_storage import RcloneError
        if isinstance(exc, (ConnectorError, WorkspaceError, RcloneError)):
            return str(exc)
        return 'Finix operation failed; check local configuration, public recipient, storage and authorization. No old copies are pruned on upload/readback failure.'
    except (ValueError, OSError, RuntimeError):
        return 'Finix operation failed; check local configuration, public recipient, storage and authorization. No old copies are pruned on upload/readback failure.'


def register(ctx):
    from hermes_constants import (get_hermes_home, set_hermes_home_override,
                                  reset_hermes_home_override)
    from .scheduler import Worker
    from .connect_commands import ConnectCommands
    import asyncio
    home = get_hermes_home().expanduser().resolve()
    from .extra_sources import trusted_root
    default_root = trusted_root()
    bound_profile = lambda: Profile(home, default_root=default_root)
    connection_commands = ConnectCommands()
    ctx.on_unload(connection_commands.close)

    def run(raw_args, **kwargs):
        token = set_hermes_home_override(home)
        try:
            return command(raw_args, owner_id=ctx.get_config('owner_id', ''),
                           owner_platform=ctx.get_config('owner_platform', ''),
                           connection_commands=connection_commands, bound_profile=bound_profile, **kwargs)
        finally:
            reset_hermes_home_override(token)

    async def handle(raw_args, **kwargs):
        # Native gateway awaits this; filesystem/process work never blocks its loop.
        return await asyncio.to_thread(run, raw_args, **kwargs)

    def owner():
        token = set_hermes_home_override(home)
        try:
            return ctx.get_config('owner_id', ''), ctx.get_config('owner_platform', '')
        finally:
            reset_hermes_home_override(token)

    ctx.register_command('finix', handler=handle, description='Owner-only encrypted profile backups',
                         args_hint='[status|setup|connect|backup|configure|recipient|restore]')
    import warnings
    if callable(getattr(ctx, 'register_telegram_handler', None)):
        from .telegram_menu import TelegramMenu
        menu = TelegramMenu(bound_profile, connection_commands, owner, handle)
        ctx.register_telegram_handler(menu.factory)
        ctx.on_unload(menu.close)
    else:
        warnings.warn('Finix Telegram menu needs the native register_telegram_handler SDK; update Hermes or use hermes finix.', RuntimeWarning)
    if callable(getattr(ctx, 'register_cli_command', None)):
        from .plugin_cli import setup_parser, run as cli_run
        ctx.register_cli_command('finix', help='Encrypted profile backup setup', setup_fn=setup_parser,
                                 handler_fn=lambda args: cli_run(args, profile_factory=bound_profile))
    else:
        warnings.warn('Finix native CLI needs register_cli_command SDK; update Hermes or use python -m finix.', RuntimeWarning)
    # Uninstalled development imports have no native credential grant or lifecycle
    # ownership. Only an actual payload in this profile may start unattended work.
    manifest = getattr(ctx, 'manifest', None)
    if manifest is not None and Path(manifest.path).resolve().is_relative_to(home / 'plugins'):
        worker = Worker(home, default_root=default_root)
        ctx.on_unload(worker.stop)
        worker.start()
