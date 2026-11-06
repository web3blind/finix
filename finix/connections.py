"""Owner-UI connection sessions. No codes/tokens are accepted or returned here.

The host authenticates the UI and binds current_profile before calling this API.
A read-issued ticket additionally prevents cross-site form/JSON CSRF. Session
workers are process-local: restart invalidates them; they self-expire on disk.
"""
import hmac
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

PROVIDERS = {'workspace': 'Google Workspace', 'yandex': 'Яндекс Диск',
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
        allowed = {'response_type', 'client_id', 'redirect_uri', 'scope', 'state', 'access_type',
                   'prompt', 'code_challenge', 'code_challenge_method', 'include_granted_scopes'}
        return (u.scheme == 'https' and u.netloc == 'accounts.google.com'
                and u.path in {'/o/oauth2/auth', '/o/oauth2/v2/auth'}
                and set(query) <= allowed and all(len(v) == 1 and v[0] for v in query.values())
                and query.get('response_type') == ['code']
                and query.get('redirect_uri') == ['http://127.0.0.1:53683/']
                and bool(query.get('state')) and bool(query.get('client_id')))
    return (u.scheme == 'http' and u.netloc == '127.0.0.1:53682' and u.path == '/auth'
            and set(query) == {'state'} and len(query['state']) == 1
            and re.fullmatch(r'[A-Za-z0-9_~.%-]{1,512}', query['state'][0]) is not None)


def workspace_prerequisites(profile, *, oauth=False):
    from importlib.util import find_spec
    from .workspace_drive import helper_path
    helper = helper_path(profile.home)
    if not helper:
        raise ConnectionError('WORKSPACE_HELPER_REQUIRED')
    dependencies = ['google.auth', 'googleapiclient']
    if oauth:
        dependencies.append('google_auth_oauthlib')
    try:
        ready = all(find_spec(name) is not None for name in dependencies)
    except (ImportError, ValueError):
        ready = False
    if not ready:
        raise ConnectionError('WORKSPACE_DEPENDENCIES_REQUIRED')
    return helper


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
                    'workspace_setup': 'Owning profile: skills/productivity/google-workspace/scripts/setup.py --client-secret /absolute/local/file.json; do not send file contents or authorization codes in chat.'}

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
        binary = shutil.which('rclone') or ''
        if provider != 'workspace':
            if not binary:
                raise ConnectionError('RCLONE_MISSING')
            info = Path(binary).stat()
            if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
                raise ConnectionError('UNTRUSTED_BINARY')
        else:
            if not (profile.home / 'google_token.json').exists() and not (profile.home / 'google_client_secret.json').is_file():
                raise ConnectionError('WORKSPACE_CLIENT_REQUIRED')
        reuse = provider == 'workspace' and (profile.home / 'google_token.json').exists()
        worker_provider = 'workspace-reuse' if reuse else provider
        if provider == 'workspace':
            helper = workspace_prerequisites(profile, oauth=not reuse)
            if reuse:
                binary = str(helper)
        ident = secrets.token_hex(16)
        stage = profile.root / 'connections' / ident
        core.private_dir(stage.parent)
        core.private_dir(stage)
        p = subprocess.Popen([sys.executable, str(Path(__file__).with_name('connection_worker.py')),
                              worker_provider, str(profile.home), str(stage), binary],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=child_env(profile.home), start_new_session=True)
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
                            labels.append({'id': str(i), 'label': label or 'Диск ' + str(i + 1)})
                        s['public'].update(status='choice', question=event['question'],
                                           choices=labels)
                        s['public'].pop('url', None)
                    elif status == 'verified':
                        verified = True
                        s['public'].update(status='working')
                        s['public'].pop('url', None)
                    elif status == 'failed':
                        s['public'].update(status='failed')
                        if event.get('code') == 'WORKSPACE_REFRESH_TOKEN_REQUIRED':
                            s['public']['code'] = 'WORKSPACE_REFRESH_TOKEN_REQUIRED'
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
            p.stdout.close()
            p.stdin.close()

    def _stop(self, s, status):
        p = s['process']
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        p.wait()
        s['public'] = {k: v for k, v in s['public'].items() if k in {'id', 'provider', 'folder', 'code'}}
        s['public']['status'] = status
        shutil.rmtree(s['stage'], ignore_errors=True)

    def _apply(self, profile, s):
        with core.locked(profile.root):
            if profile.config() != s['before']:
                raise ConnectionError('SETTINGS_CHANGED')
            cfg = profile.config()
            if s['public']['provider'] == 'workspace':
                workspace_prerequisites(profile, oauth=not s['reuse'])
                target = profile.home / 'google_token.json'
                if not s['reuse'] and target.exists():
                    raise ConnectionError('WORKSPACE_REUSE_EXISTING')
                # Link is exclusive: never overwrite an existing Workspace account.
                if not s['reuse']:
                    os.link(s['stage'] / 'workspace-token.json', target, follow_symlinks=False)
                cfg['storage'] = 'workspace'
            else:
                from .rclone_storage import config_path
                path = config_path(str(s['stage'] / 'rclone.conf'))
                cfg.update(storage='rclone', rclone_config=str(path), rclone_remote='finix',
                           rclone_root=s['public']['folder'], rclone_connection=s['public']['id'])
            core.atomic_json(profile.root / 'settings.json', cfg)
            if profile.config() != cfg:
                raise ConnectionError('READBACK_FAILED')
            s['public']['status'] = 'applied'
            s['public'].pop('url', None)


connections = Connections()
