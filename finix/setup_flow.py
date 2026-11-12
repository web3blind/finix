"""Shared setup state; callers authorize and bind the owning Profile.

A receipt proves a completed encrypted backup and the selected adapter's full
readback, not possession of the recovery identity or future cloud availability.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import time

from . import core
from .extra_sources import read_config, records


def parse_period(period):
    """Strict whole hours/days, bounded by Profile.configure's one-year maximum."""
    if not isinstance(period, str) or not re.fullmatch(r'[1-9][0-9]{0,4}[hd]', period):
        raise core.FinixError('Period must be a positive integer followed by h or d, e.g. 24h or 7d')
    hours = int(period[:-1]) * (24 if period[-1] == 'd' else 1)
    if hours > 8760:
        raise core.FinixError('Period exceeds 8760 hours')
    return hours


def _stamp(path):
    """Only metadata: credential contents never enter this service."""
    try:
        return core.fingerprint(Path(path).lstat())
    except FileNotFoundError:
        return None


class SetupFlow:
    def __init__(self, profile):
        self.profile = profile
        self.receipt_path = profile.root / 'setup-verification.json'

    def _binding(self, cfg=None):
        cfg = self.profile.config() if cfg is None else cfg
        # Operational scheduling changes must not invalidate their own receipt.
        settings = {k: v for k, v in cfg.items() if k not in {'interval_hours', 'next_run'}}
        config, _ = read_config(self.profile.home)
        files = {}
        if cfg['storage'] == 'rclone' and cfg.get('rclone_config'):
            # Rclone owns refresh and may replace the token file atomically.
            # Bind the selected logical connection and its private parent,
            # never credential bytes, inode, size or refresh timestamps. The
            # transport ledger independently binds the validated remote type.
            parent = Path(cfg['rclone_config']).parent
            try:
                st = parent.stat()
                files['rclone_parent'] = (st.st_dev, st.st_ino)
            except FileNotFoundError:
                files['rclone_parent'] = None
        if cfg['storage'] == 'workspace':
            parent = self.profile.home
            st = parent.stat()
            files['workspace_parent'] = (st.st_dev, st.st_ino)
        binding = {'profile_id': self.profile.profile_id, 'settings': settings,
                   'extra_sources': records(config), 'credential_metadata': files}
        return hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _receipt(self, binding):
        try:
            with core.regular_input(self.receipt_path) as fd:
                receipt = json.loads(core.read_regular(fd, 16384))
        except (FileNotFoundError, ValueError, core.FinixError):
            return None
        if (not isinstance(receipt, dict) or receipt.get('profile_id') != self.profile.profile_id
                or receipt.get('binding') != binding or receipt.get('version') != 1):
            return None
        return receipt

    def view(self):
        cfg = self.profile.config()
        receipt = self._receipt(self._binding(cfg))
        capture = self.profile.capture_status()
        if capture and not capture['complete']:
            receipt = None
        present = bool(cfg.get('recipient'))
        stage = 'key' if not present else 'check'
        if present and cfg['storage'] != 'local':
            required = {'rclone': ('rclone_config', 'rclone_remote', 'rclone_root'),
                        'connector-drive': ('connector_account', 'connector_alias', 'connector_folder')}
            if any(not cfg.get(k) for k in required.get(cfg['storage'], ())):
                stage = 'cloud'
        if receipt:
            stage = 'complete' if cfg.get('interval_hours') else 'schedule'
        return {'profile_id': self.profile.profile_id, 'recipient_present': present,
                'recipient_locked': present, 'recipient': cfg.get('recipient'),
                'stage': stage, 'verified': bool(receipt), 'capture': capture,
                'interval_hours': cfg.get('interval_hours', 0), 'storage': cfg['storage'],
                'storage_label': 'Local only (not a cloud backup)' if cfg['storage'] == 'local' else cfg['storage'],
                'proof': receipt, 'key_instructions':
                'The server stores only the public age recipient, never your private key. '
                'If absent, create and save the recovery key on your own device using the offline '
                'dist/keygen.html or Desktop client; register only its public age1 recipient. '
                'An existing registered recipient cannot be replaced.',
                'cloud_instructions': 'Choose workspace, yandex, onedrive or dropbox using connect; '
                'choose local explicitly for local-only copies. Check runs the actual first backup '
                'and cloud readback before enabling a period.'}

    def key(self):
        view = self.view()
        if not view['recipient_present']:
            from .assets import key_page
            page = key_page()
            view['offline_page_path'] = str(page)
            view['offline_page_url'] = page.as_uri()
        return view

    def check_and_run(self, progress=None, cancel=None, *, resume=False):
        # Never leave an earlier success usable after a new failed check.
        with core.locked(self.profile.directories()):
            self.receipt_path.unlink(missing_ok=True)
            before_cfg = self.profile.config()
            if before_cfg.get('storage') not in {'local', 'drive', 'workspace', 'rclone'}:
                raise core.FinixError('Select a supported storage backend before checking')
            before = self._binding(before_cfg)
            yaml_before = _stamp(self.profile.home / 'config.yaml')
            if not before_cfg.get('recipient'):
                raise core.FinixError('Register a public recipient on your device first')
            # Rechecking is an explicit setup transition: pause previous scheduling
            # until this backup succeeds and the owner supplies a period again.
            if before_cfg.get('interval_hours') or before_cfg.get('next_run'):
                before_cfg['interval_hours'] = 0
                before_cfg['next_run'] = 0
                core.atomic_json(self.profile.root / 'settings.json', before_cfg)
        # Profile owns encryption, source validation, upload/readback and retention.
        # Its lock is intentionally non-reentrant, so do not nest that lock here.
        options = {}
        if progress is not None: options['progress'] = progress
        if cancel is not None: options['cancel'] = cancel
        if resume: options['resume'] = resume
        result = self.profile.backup(**options)
        with core.locked(self.profile.directories()), core.locked(self.profile.home):
            after_cfg = self.profile.config()
            # Legacy Drive allocates the folder on its first successful upload.
            expected_cfg = dict(before_cfg)
            if (before_cfg['storage'] == 'drive' and not before_cfg.get('folder_id')
                    and result.get('drive')):
                expected_cfg['folder_id'] = after_cfg.get('folder_id')
                before = self._binding(expected_cfg)
            if (self._binding(after_cfg) != before
                    or _stamp(self.profile.home / 'config.yaml') != yaml_before):
                raise core.FinixError('Setup changed during backup; check and run again')
            path = Path(result['path'])
            digest = core.digest_file(path, cancel=cancel, progress=progress)
            if result.get('sha256') and result['sha256'] != digest:
                raise core.FinixError('Completed archive changed; check and run again')
            receipt = {'version': 1, 'profile_id': self.profile.profile_id,
                       'binding': before, 'checked_at': time.time(),
                       'archive': path.name, 'sha256': digest, 'storage': after_cfg['storage'],
                       'cloud_readback': after_cfg['storage'] != 'local'}
            # Recheck after the archive I/O, before committing proof.
            if self._binding() != before or _stamp(self.profile.home / 'config.yaml') != yaml_before:
                raise core.FinixError('Setup changed during verification; check and run again')
            if result.get('complete') is True:
                core.atomic_json(self.receipt_path, receipt)
        result['verified'] = True
        return {**self.view(), 'first_backup': result}

    def schedule(self, period):
        hours = parse_period(period)
        with core.locked(self.profile.directories()), core.locked(self.profile.home):
            cfg = self.profile.config()
            binding = self._binding(cfg)
            capture = self.profile.capture_status()
            if (capture and not capture['complete']) or not self._receipt(binding):
                raise core.FinixError('Check and run a successful first backup before scheduling')
            if self._binding() != binding:
                raise core.FinixError('Setup changed; check and run again')
            cfg['interval_hours'] = hours
            cfg['next_run'] = time.time() + hours * 3600
            core.atomic_json(self.profile.root / 'settings.json', cfg)
        return self.view()
