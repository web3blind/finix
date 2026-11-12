"""Owner-UI connection sessions. No codes/tokens are accepted or returned here.

The host authenticates the UI and binds current_profile before calling this API.
A read-issued ticket additionally prevents cross-site form/JSON CSRF. Session
workers are process-local: restart invalidates them; they self-expire on disk.
"""
import hmac
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs

from . import core
from .rclone_storage import child_env, relative_root

PROVIDERS = {'workspace': 'Google Drive', 'yandex': 'Yandex Disk',
             'onedrive': 'OneDrive', 'dropbox': 'Dropbox'}
TTL = 300


class ConnectionError(core.FinixError):
    def __init__(self, code):
        self.code = code
        super().__init__('Finix connection: ' + code)


def safe_url(provider, url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        return False
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if u.username or u.password or u.fragment:
        return False
    query = parse_qs(u.query, keep_blank_values=True)
    if any(k in query for k in ('code', 'access_token', 'refresh_token', 'id_token')):
        return False
    if provider == 'workspace':
        return False  # Hermes owns Google authorization; Finix never exposes a callback.
    return (u.scheme == 'http' and u.netloc == '127.0.0.1:53682' and u.path == '/auth'
            and set(query) == {'state'} and len(query['state']) == 1
            and re.fullmatch(r'[A-Za-z0-9_~.%-]{1,512}', query['state'][0]) is not None)


class Connections:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}
        self.tickets = {}

    def view(self, profile):
        with self.lock:
            self._expire()
            entry = self.tickets.get(profile.profile_id)
            if not entry or entry[1] < time.monotonic():
                entry = (secrets.token_urlsafe(32), time.monotonic() + TTL)
                self.tickets[profile.profile_id] = entry
            session = self.sessions.get(profile.profile_id)
            public = dict(session['public']) if session else {'status': 'idle'}
            return {'profile_id': profile.profile_id, 'ticket': entry[0],
                    'providers': [{'id': k, 'label': v} for k, v in PROVIDERS.items()],
                    'session': public, 'local_only': True,
                    'workspace_setup': 'Run hermes auth add finix:google in this profile. Hermes owns login and credentials. Existing Workspace data is not adopted automatically.'}

    def _expire(self):
        for key, s in list(self.sessions.items()):
            if time.monotonic() >= s['deadline'] and s['public']['status'] not in {'applied', 'cancelled', 'expired'}:
                self._stop(s, 'expired')

    def _check(self, profile, profile_id, ticket):
        entry = self.tickets.get(profile.profile_id)
        if (profile_id != profile.profile_id or not entry or entry[1] < time.monotonic()
                or not hmac.compare_digest(entry[0], ticket)):
            raise ConnectionError('OWNER_SESSION_CHANGED')
        self._expire()

    def action(self, profile, *, profile_id, ticket, action, provider='', session_id='',
               folder='Finix', confirmed=False, local_browser=False, choice=''):
        with self.lock:
            self._check(profile, profile_id, ticket)
            if action == 'start':
                self._start(profile, provider, folder, confirmed, local_browser)
            else:
                s = self.sessions.get(profile.profile_id)
                if not s or not hmac.compare_digest(s['public']['id'], session_id):
                    raise ConnectionError('SESSION_CHANGED')
                if action == 'cancel' and s['public']['status'] != 'applied':
                    self._stop(s, 'cancelled')
                elif action == 'answer' and s['public']['status'] == 'choice':
                    allowed = [c['id'] for c in s['public']['choices']]
                    if choice not in allowed:
                        raise ConnectionError('INVALID_CHOICE')
                    s['process'].stdin.write(json.dumps(int(choice)).encode() + b'\n')
                    s['process'].stdin.flush()
                    s['public'] = {**s['public'], 'status': 'working', 'choices': []}
                elif action == 'apply' and s['public']['status'] == 'verified':
                    self._apply(profile, s)
                else:
                    raise ConnectionError('INVALID_TRANSITION')
            return self.view(profile)

    def _start(self, profile, provider, folder, confirmed, local_browser):
        if provider not in PROVIDERS:
            raise ConnectionError('UNKNOWN_PROVIDER')
        if not confirmed:
            raise ConnectionError('CONSENT_REQUIRED')
        if not local_browser:
            raise ConnectionError('LOCAL_BROWSER_REQUIRED')
        folder = relative_root(folder)
        # Fixed rclone callback port and Google credential activation must not be
        # raced by a second local wizard, even on another owning profile.
        if any(s['public']['status'] in {'working', 'authorize', 'choice', 'verified'}
               for s in self.sessions.values()):
            raise ConnectionError('CONNECTION_BUSY')
        if provider == 'workspace':
            from .workspace_drive import WorkspaceRunner
            try:
                account = WorkspaceRunner(profile.home)('identity')['account_id']
                if not isinstance(account, str) or not account:
                    raise ValueError
            except Exception:
                raise ConnectionError('GOOGLE_AUTH_SETUP_REQUIRED') from None
            ident = secrets.token_hex(16)
            self.sessions[profile.profile_id] = {
                'public': {'id': ident, 'provider': provider, 'status': 'verified', 'folder': folder, 'expires_in': TTL},
                'deadline': time.monotonic() + TTL, 'before': profile.config(), 'process': None, 'stage': None,
                'account_digest': hashlib.sha256(account.encode()).hexdigest()}
            return
        binary = ''
        if provider != 'workspace':
            if os.name == 'nt':
                from .windows_native import find_tool
                try:
                    binary = find_tool('rclone')
                except FileNotFoundError:
                    raise ConnectionError('RCLONE_MISSING') from None
                except OSError:
                    raise ConnectionError('UNTRUSTED_BINARY') from None
            else:
                binary = shutil.which('rclone') or ''
                if not binary:
                    raise ConnectionError('RCLONE_MISSING')
                info = Path(binary).stat()
                if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
                    raise ConnectionError('UNTRUSTED_BINARY')
        reuse = False
        worker_provider = provider
        ident = secrets.token_hex(16)
        stage = profile.root / 'connections' / ident
        core.private_dir(stage.parent)
        core.private_dir(stage)
        if os.name == 'nt':
            from .windows_native import popen
        else:
            popen = subprocess.Popen
        from .workspace_drive import trusted_runtime, WorkspaceError
        worker = Path(__file__).absolute().with_name('connection_worker.py')
        try:
            with trusted_runtime(Path(binary) if reuse else worker, entry=worker) as (prefix, wrapper):
                p = popen([*prefix, wrapper, worker_provider, str(profile.home), str(stage), binary],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env=child_env(profile.home), start_new_session=True)
        except WorkspaceError:
            raise ConnectionError('UNTRUSTED_BINARY') from None
        s = {'public': {'id': ident, 'provider': provider, 'status': 'working', 'folder': folder,
                        'expires_in': TTL}, 'stage': stage, 'process': p,
             'deadline': time.monotonic() + TTL, 'before': profile.config(), 'reuse': reuse}
        self.sessions[profile.profile_id] = s
        threading.Thread(target=self._read, args=(s,), daemon=True).start()
        timer = threading.Timer(TTL, self._timeout, args=(s,))
        timer.daemon = True
        timer.start()

    def _timeout(self, s):
        with self.lock:
            if s['public']['status'] not in {'applied', 'cancelled', 'failed', 'expired'}:
                self._stop(s, 'expired')

    def _read(self, s):
        p = s['process']
        verified = False
        try:
            for _ in range(40):
                line = p.stdout.readline(16385)
                if not line:
                    break
                if len(line) > 16384:
                    raise ValueError('limit')
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('event')
                with self.lock:
                    if s['public']['status'] in {'cancelled', 'expired', 'applied'}:
                        break
                    status = event.get('status')
                    if status == 'authorize' and safe_url(s['public']['provider'], event.get('url')):
                        s['public'].update(status='authorize', url=event['url'])
                    elif status == 'choice' and event.get('question') == 'config_driveid':
                        choices = event.get('choices')
                        if (not isinstance(choices, list) or not 1 <= len(choices) <= 30
                                or any(not isinstance(c, dict) for c in choices)):
                            raise ValueError('choice')
                        labels = []
                        for i, choice in enumerate(choices):
                            label = str(choice.get('label', ''))[:120]
                            if any(x in label.lower() for x in ('http', 'token', 'secret', 'code=')):
                                label = ''
                            label = ''.join(c for c in label if c.isprintable())
                            labels.append({'id': str(i), 'label': label or 'Drive ' + str(i + 1)})
                        s['public'].update(status='choice', question=event['question'],
                                           choices=labels)
                        s['public'].pop('url', None)
                    elif status == 'verified':
                        verified = True
                        s['public'].update(status='working')
                        s['public'].pop('url', None)
                    elif status == 'failed':
                        s['public'].update(status='failed')
                        allowed = {'WORKSPACE_REFRESH_TOKEN_REQUIRED', 'UNTRUSTED_BINARY',
                                   'RCLONE_PROVIDER_FAILED', 'CONNECTION_UNSUPPORTED',
                                   'CONNECTION_TIMEOUT', 'CONNECTION_FAILED'}
                        code = event.get('code')
                        s['public']['code'] = code if isinstance(code, str) and code in allowed else 'CONNECTION_FAILED'
                        s['public'].pop('url', None)
                    else:
                        raise ValueError('event')
            rc = p.wait(timeout=5)
            with self.lock:
                if s['public']['status'] not in {'cancelled', 'expired', 'applied'}:
                    if not rc and verified and s['public']['status'] == 'working':
                        s['public']['status'] = 'verified'
                    else:
                        self._stop(s, 'failed')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            with self.lock:
                self._stop(s, 'failed')
        finally:
            if os.name == 'nt':
                from .windows_native import cleanup
                cleanup(p)
            p.stdout.close()
            p.stdin.close()

    def _stop(self, s, status):
        p = s['process']
        if p is None:
            s['public']['status'] = status
            return
        if os.name == 'nt':
            from .windows_native import cleanup
            cleanup(p)
        elif p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        p.wait()
        s['public'] = {k: v for k, v in s['public'].items() if k in {'id', 'provider', 'folder', 'code'}}
        s['public']['status'] = status
        shutil.rmtree(s['stage'], ignore_errors=True)

    def _apply(self, profile, s):
        if s['public']['provider'] == 'workspace':
            from .workspace_drive import WorkspaceRunner
            try:
                account = WorkspaceRunner(profile.home)('identity')['account_id']
                digest = hashlib.sha256(account.encode()).hexdigest()
            except Exception:
                raise ConnectionError('GOOGLE_AUTH_SETUP_REQUIRED') from None
            if not hmac.compare_digest(digest, s.get('account_digest', '')):
                raise ConnectionError('GOOGLE_ACCOUNT_CHANGED')
        from .backup_jobs import BackupJobs, state_lease, file_lease
        from .setup_flow import SetupFlow
        jobs = BackupJobs()
        root, state_path, worker = jobs._paths(profile)
        # Same lock order as Start; reserve the worker lease across settings CAS.
        # Even an admitted worker not yet holding the operation lock blocks Apply.
        with state_lease(root / 'state.lock'), file_lease(worker), core.locked(profile.root):
            if profile.config() != s['before']:
                raise ConnectionError('SETTINGS_CHANGED')
            cfg = profile.config()
            before_binding = SetupFlow(profile)._binding(cfg)
            job = jobs._read(profile, state_path)['job']
            owned = bool(job and (job['state'] == 'interrupted'
                                  or job['state'] == 'failed' and job.get('capture')))
            if owned and job.get('binding') != before_binding:
                if not jobs._transition(profile, job, before_binding):
                    raise ConnectionError('SETTINGS_CHANGED')
            if s['public']['provider'] == 'workspace':
                cfg['storage'] = 'workspace'
                cfg['workspace_account'] = digest
            else:
                from .rclone_storage import config_path
                path = config_path(str(s['stage'] / 'rclone.conf'))
                cfg.update(storage='rclone', rclone_config=str(path), rclone_remote='finix',
                           rclone_root=s['public']['folder'], rclone_connection=s['public']['id'])
            if job:
                # Checkpoint and old setup receipt are durable before the settings
                # switch. Old provider pending/ledger/ciphertext stay in place.
                jobs._archive(root, job)
                if job.get('capture') and job['state'] != 'succeeded':
                    capture = job['capture']
                    name = capture['archive']
                    if not isinstance(name, str) or Path(name).name != name or '\\' in name or name in {'.', '..'}:
                        raise ConnectionError('SETTINGS_CHANGED')
                    pins = core.private_dir(root / 'retained-captures')
                    core.atomic_json(pins / (name + '.json'), {
                        'job_id': job['id'], 'binding': job['binding'],
                        'archive': name, 'sha256': capture['sha256']})
                evidence = {'settings': s['before']}
                try:
                    with core.regular_input(profile.root / 'setup-verification.json') as fd:
                        evidence['setup_receipt'] = json.loads(core.read_regular(fd, 16384))
                except FileNotFoundError:
                    pass
                evidence_path = jobs._receipt_path(root, job['id'], 'history-bindings')
                if not evidence_path.exists():
                    core.atomic_json(evidence_path, evidence)
                core.atomic_json(profile.root / 'connection-transition.json', {
                    'version': 1, 'profile_id': profile.profile_id, 'job_id': job['id'],
                    'from_binding': job['binding'], 'to_binding': SetupFlow(profile)._binding(cfg)})
            core.atomic_json(profile.root / 'settings.json', cfg)
            if profile.config() != cfg:
                raise ConnectionError('READBACK_FAILED')
            s['public']['status'] = 'applied'
            s['public'].pop('url', None)


connections = Connections()
