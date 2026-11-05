"""Profile-bound orchestration. No secret keys or auth state are read here."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import time

from . import core


class Profile:
    def __init__(self, home):
        self.home = core.checked_path(home)
        binding = hashlib.sha256(str(self.home).encode()).hexdigest()
        # Random backend identity prevents same-path profiles on different servers
        # accepting stale recipient POSTs. It is not part of the restored profile.
        self.root = self.home.parent / 'finix-data' / binding
        core.private_dir(self.root)
        with core.locked(self.root):
            identity = self.root / 'backend.json'
            if not identity.exists():
                import secrets
                core.atomic_json(identity, {'profile_id': secrets.token_hex(32)})
            fd = os.open(identity, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                self.profile_id = json.loads(core.read_regular(fd, 1024))['profile_id']
            finally:
                os.close(fd)

    def directories(self):
        core.private_dir(self.root)
        return self.root

    def config(self):
        path = self.root / 'settings.json'
        if not path.exists():
            return {'recipient': None, 'storage': 'local', 'retention': 0, 'interval_hours': 0,
                    'next_run': 0, 'folder_id': None}
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            return json.loads(core.read_regular(fd, 65536))
        finally:
            os.close(fd)

    def status(self):
        cfg = self.config()
        return {'profile_id': self.profile_id, **cfg,
                'scheduler': 'automatic while native plugin is loaded; optional python -m finix.scheduler when host is offline'}

    def recipient(self, profile_id, recipient, expected):
        if profile_id != self.profile_id:
            raise core.FinixError('Profile changed; reload status')
        if not isinstance(recipient, str) or not core.RECIPIENT.fullmatch(recipient):
            raise core.FinixError('Expected one native age public recipient')
        with core.locked(self.directories()):
            cfg = self.config()
            if cfg['recipient'] != expected:
                raise core.FinixError('Recipient changed; reload and confirm the current recipient')
            cfg['recipient'] = recipient
            core.atomic_json(self.root / 'settings.json', cfg)
        return self.status()

    def configure(self, key, value):
        with core.locked(self.directories()):
            cfg = self.config()
            if key in {'retention', 'interval_hours'}:
                value = int(value)
                if not 0 <= value <= (1000 if key == 'retention' else 8760):
                    raise core.FinixError('Configuration value out of range')
                if key == 'interval_hours':
                    cfg['next_run'] = time.time() + value * 3600 if value else 0
            elif key == 'storage':
                if value not in {'local', 'drive'}:
                    raise core.FinixError('Storage must be local or drive')
            else:
                raise core.FinixError('Allowed settings: storage, retention, interval_hours')
            cfg[key] = value
            core.atomic_json(self.root / 'settings.json', cfg)
        return self.status()

    def backup(self, scheduled=False):
        with core.locked(self.directories()):
            cfg = self.config()
            now = time.time()
            if scheduled and (not cfg['interval_hours'] or cfg['next_run'] > now):
                return {'skipped': True}
            if not cfg['recipient']:
                raise core.FinixError('Register a public recipient first')
            # Do not prune local copies until cloud readback has succeeded.
            result = core.backup(self.home, self.root / 'copies', self.root / 'index', cfg['recipient'])
            if cfg['storage'] == 'drive':
                from .drive import Drive
                drive = Drive(self.profile_id)
                if not cfg['folder_id']:
                    cfg['folder_id'] = drive.create_folder()
                    core.atomic_json(self.root / 'settings.json', cfg)
                result['drive'] = drive.upload(Path(result['path']), cfg['folder_id'], cfg['retention'])
            if cfg['retention']:
                self._prune_local(cfg['retention'])
            cfg['next_run'] = now + cfg['interval_hours'] * 3600 if cfg['interval_hours'] else 0
            core.atomic_json(self.root / 'settings.json', cfg)
            return result

    def _prune_local(self, keep):
        state, output = self.root / 'index', self.root / 'copies'
        with core.locked(state), core.locked(output):
            index = core.load_state(state)
            old = index['backups'][:-keep]
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


def command(raw_args, *, owner_id='', owner_platform='', platform='', source=None, **kwargs):
    """Only adapter-attested identities may use chat operations, including status."""
    if (not owner_id or not owner_platform or source is None or platform != owner_platform
            or str(source.user_id) != str(owner_id)):
        return 'Finix denied: configure owner_id and owner_platform locally; trusted owner context required.'
    try:
        args = shlex.split(raw_args)
        profile = current_profile()
        if not args or args == ['status']:
            return json.dumps(profile.status())
        if args == ['setup']:
            return ('Generate and save your private key only on your device: open the shipped '
                    'dist/keygen.html offline (authenticated backend download: '
                    '/api/plugins/finix/keygen.html). Send only the public age1 recipient. '
                    'Use /finix recipient NEW none for initial registration; '
                    '/finix recipient NEW OLD confirm for replacement. '
                    'Cloud login: hermes auth add finix:google (owner terminal only).')
        if args[0] == 'recipient' and len(args) in {3, 4}:
            expected = None if args[2] == 'none' else args[2]
            if expected is not None and args[3:] != ['confirm']:
                raise core.FinixError('Replacement requires OLD recipient and confirm')
            return json.dumps(profile.recipient(profile.profile_id, args[1], expected))
        if args == ['backup']:
            return json.dumps(profile.backup())
        if args[0] == 'configure' and len(args) == 3:
            return json.dumps(profile.configure(args[1], args[2]))
        if args == ['restore']:
            return 'Restore only on your recovery device: python -m finix restore --archive FILE --identity KEYFILE --target NEW_DIRECTORY. Never send a private key to chat.'
        return 'Usage: /finix status|setup|recipient NEW none|recipient NEW OLD confirm|backup|configure storage local/drive|configure retention N|configure interval_hours N|restore'
    except (core.FinixError, ValueError, OSError, RuntimeError):
        return 'Finix operation failed; check local configuration, public recipient, storage and authorization. No old copies are pruned on upload/readback failure.'


def register(ctx):
    from hermes_constants import (get_hermes_home, set_hermes_home_override,
                                  reset_hermes_home_override)
    from .scheduler import Worker
    home = get_hermes_home().expanduser().resolve()

    def handle(raw_args, **kwargs):
        token = set_hermes_home_override(home)
        try:
            return command(raw_args, owner_id=ctx.get_config('owner_id', ''),
                           owner_platform=ctx.get_config('owner_platform', ''), **kwargs)
        finally:
            reset_hermes_home_override(token)

    ctx.register_command('finix', handler=handle, description='Owner-only encrypted profile backups',
                         args_hint='[status|setup|backup|configure|recipient|restore]')
    # Uninstalled development imports have no native credential grant or lifecycle
    # ownership. Only an actual payload in this profile may start unattended work.
    if Path(ctx.manifest.path).resolve().is_relative_to(home / 'plugins'):
        worker = Worker(home)
        ctx.on_unload(worker.stop)
        worker.start()
