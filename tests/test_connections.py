"""Connection tests use synthetic homes and real child pipes, never cloud accounts."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from finix.connections import Connections, ConnectionError, safe_url
from finix.integration import Profile, command

FAKE_RCLONE = r'''#!/usr/bin/env python3
import json,sys,secrets
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs
args=sys.argv
config=Path(args[args.index('--config')+1])
if 'create' in args:
 config.write_text('[finix]\ntype = yandex\n')
 print(json.dumps({'State':'initial-state','Option':{'Name':'config_is_local'}}))
elif 'update' in args:
 state=secrets.token_urlsafe(24)
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*a): pass
  def do_GET(self):
   q=parse_qs(urlsplit(self.path).query)
   good=q.get('state')==[state] and q.get('code')==['SYNTHETIC_GRANT']
   self.send_response(200 if good else 403); self.end_headers()
   if good: self.server.complete=True
 server=HTTPServer(('127.0.0.1',53682),Handler); server.complete=False
 print('Please go to http://127.0.0.1:53682/auth?state='+state,file=sys.stderr,flush=True)
 while not server.complete: server.handle_request()
 server.server_close()
 config.write_text('[finix]\ntype = yandex\ntoken = SYNTHETIC_TOKEN_DO_NOT_LEAK\n')
 print(json.dumps({'State':''}))
elif 'lsjson' in args: print('{"IsDir":true}')
else: sys.exit(1)
'''


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        if os.name == 'nt':
            self.skipTest('POSIX shebang rclone fixture cannot execute on Windows; real OAuth forbidden')
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'profile'
        self.home.mkdir(mode=0o700)
        self.profile = Profile(self.home)
        self.manager = Connections()
        self.bin = self.base / 'bin'
        self.bin.mkdir(mode=0o700)
        (self.bin / 'rclone').write_text(FAKE_RCLONE)
        (self.bin / 'rclone').chmod(0o700)
        self.env = patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + os.environ['PATH']})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Fail closed before any worker can receive a real provider binary.
        original_which = shutil.which
        def fixture_only_binary(tool, *args, **kwargs):
            binary = original_which(tool, *args, **kwargs)
            if tool == 'rclone':
                self.assertIsNotNone(binary, 'synthetic rclone missing; refusing provider process')
                self.assertEqual(Path(binary).resolve(), (self.bin / 'rclone').resolve(),
                                 'real rclone forbidden in synthetic OAuth fixtures')
            return binary
        guard = patch('finix.connections.shutil.which', side_effect=fixture_only_binary)
        guard.start()
        self.addCleanup(guard.stop)
        fixture_only_binary('rclone')
        self.addCleanup(self.stop)
        self.view = self.manager.view(self.profile)

    def stop(self):
        for s in self.manager.sessions.values():
            if s['public']['status'] != 'applied':
                self.manager._stop(s, 'cancelled')

    def act(self, action, **kw):
        view = self.manager.view(self.profile)
        return self.manager.action(self.profile, profile_id=view['profile_id'], ticket=view['ticket'],
                                   action=action, session_id=view['session'].get('id', ''), **kw)

    def wait(self, status):
        # Several real interpreters start here; allow a loaded shared host,
        # independently of the production session's hard 300-second lifetime.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.manager.view(self.profile)
            if result['session']['status'] == status:
                return result
            if result['session']['status'] == 'failed':
                self.fail('connection worker failed')
            time.sleep(.02)
        self.fail('worker did not reach ' + status + '; state=' + result['session']['status'])

    def start(self):
        return self.act('start', provider='yandex', confirmed=True, local_browser=True)

    def complete(self):
        self.start()
        view = self.wait('authorize')
        with self.assertRaises(HTTPError) as denied:
            urlopen(view['session']['url'].replace('state=', 'wrong=') + '&code=SYNTHETIC_GRANT', timeout=2)
        denied.exception.close()
        self.assertEqual(self.manager.view(self.profile)['session']['status'], 'authorize')
        with urlopen(view['session']['url'] + '&code=SYNTHETIC_GRANT', timeout=2) as response:
            self.assertEqual(response.status, 200)
        self.wait('verified')
        return view

    def test_real_child_callback_state_and_explicit_atomic_activation(self):
        self.complete()
        self.assertEqual(self.profile.config()['storage'], 'local')
        view = self.act('apply')
        self.assertEqual(view['session']['status'], 'applied')
        cfg = self.profile.config()
        self.assertEqual(cfg['storage'], 'rclone')
        self.assertEqual(cfg['rclone_root'], 'Finix')
        self.assertEqual(Path(cfg['rclone_config']).stat().st_mode & 0o777, 0o600)
        self.assertNotIn('SYNTHETIC_TOKEN', json.dumps(view))
        self.assertNotIn('SYNTHETIC_GRANT', json.dumps(view))
        with self.assertRaises(ConnectionError): self.act('apply')
        with self.assertRaises(ConnectionError): self.act('cancel')
        self.assertTrue(Path(cfg['rclone_config']).exists())

    def test_cancel_expiry_restart_and_wrong_session(self):
        self.start()
        self.wait('authorize')
        s = self.manager.sessions[self.profile.profile_id]
        with self.assertRaises(ConnectionError):
            self.manager.action(self.profile, profile_id=self.profile.profile_id, ticket=self.view['ticket'], action='cancel', session_id='wrong')
        self.act('cancel')
        self.assertFalse(s['stage'].exists())
        self.assertIsNotNone(s['process'].poll())
        self.assertEqual(self.profile.config()['storage'], 'local')
        self.assertEqual(Connections().view(self.profile)['session']['status'], 'idle')
        self.start()
        self.wait('authorize')
        s = self.manager.sessions[self.profile.profile_id]
        s['deadline'] = 0
        self.assertEqual(self.manager.view(self.profile)['session']['status'], 'expired')
        self.assertFalse(s['stage'].exists())

    def test_cas_preserves_selected_storage(self):
        self.complete()
        self.profile.configure('retention', '2')
        with self.assertRaises(ConnectionError): self.act('apply')
        self.assertEqual(self.profile.config()['storage'], 'local')

    def test_owner_profile_csrf_provider_consent_remote_and_no_secret_input(self):
        for changes in ({'profile_id': 'other'}, {'ticket': 'bad'}):
            body = dict(profile_id=self.profile.profile_id, ticket=self.view['ticket'], action='start', provider='yandex', confirmed=True, local_browser=True)
            body.update(changes)
            with self.assertRaises(ConnectionError): self.manager.action(self.profile, **body)
        for options in ({'provider': 'invalid', 'confirmed': True, 'local_browser': True},
                        {'provider': 'yandex', 'confirmed': False, 'local_browser': True},
                        {'provider': 'yandex', 'confirmed': True, 'local_browser': False},
                        {'provider': 'workspace', 'confirmed': True, 'local_browser': True}):
            with self.assertRaises(ConnectionError): self.act('start', **options)
        self.assertEqual(self.profile.config()['storage'], 'local')
        self.start()
        other = Profile(self.base / 'other')
        view = self.manager.view(other)
        self.assertEqual(view['session']['status'], 'idle')
        with self.assertRaises(ConnectionError):
            self.manager.action(other, profile_id=other.profile_id, ticket=view['ticket'], action='start', provider='yandex', confirmed=True, local_browser=True)

    def test_safe_url_filter(self):
        self.assertTrue(safe_url('yandex', 'http://127.0.0.1:53682/auth?state=safe'))
        for u in ('https://evil.example/auth?state=safe', 'http://127.0.0.1:1/auth?state=safe',
                  'http://127.0.0.1:53682/auth?state=safe&code=secret',
                  'http://user@127.0.0.1:53682/auth?state=safe'):
            self.assertFalse(safe_url('yandex', u))

    def public_google(self, *, signed_in=True, account=None):
        """Real SDK request API; fake credential-store read + HTTPX MockTransport.

        The installed probe supplies genuine SDK caller attribution. Neither the
        request API nor OAuth admission is replaced, and no grant is persisted.
        """
        from contextlib import ExitStack
        import httpx
        import yaml
        from hermes_cli import plugin_oauth as oauth
        plugin = self.home / 'plugins' / 'finix'
        plugin.mkdir(parents=True, mode=0o700, exist_ok=True)
        manifest = (Path(__file__).parents[1] / 'plugin.yaml').read_bytes()
        (plugin / 'plugin.yaml').write_bytes(manifest)
        clients, problems = oauth.parse_oauth_clients(yaml.safe_load(manifest))
        self.assertEqual(problems, [])
        fingerprint = clients[0].fingerprint
        namespace = {}
        exec(compile('from hermes_cli.plugin_provider_requests import credentialed_provider_request\n'
                     'def request(method, url, **kwargs):\n'
                     ' return credentialed_provider_request("google", method, url, **kwargs)\n',
                     str(plugin / 'probe.py'), 'exec'), namespace)
        state = {'account': 'synthetic-account', 'calls': []} if account is None else account
        real_client = httpx.Client
        def transport(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.host, 'www.googleapis.com')
            self.assertEqual(request.url.path, '/drive/v3/about')
            self.assertEqual(request.headers['authorization'], 'Bearer SYNTHETIC_OFFLINE_GRANT')
            state['calls'].append(str(request.url))
            return httpx.Response(200, json={'user': {'permissionId': state['account']}})
        def offline_client(*args, **kwargs):
            return real_client(*args, **kwargs, transport=httpx.MockTransport(transport))
        def grant(key):
            self.assertEqual(key, 'finix:google')
            return {'fingerprint': fingerprint, 'access_token': 'SYNTHETIC_OFFLINE_GRANT'} if signed_in else None
        stack = ExitStack()
        stack.enter_context(patch.object(oauth, '_read_grant', grant))
        stack.enter_context(patch.object(httpx, 'Client', offline_client))
        stack.enter_context(patch('finix.drive.managed_request', side_effect=namespace['request']))
        return stack

    def test_workspace_existing_profile_reuse_live_probe_no_token_copy(self):
        import hashlib
        token = self.home / 'google_token.json'
        token.write_text('SYNTHETIC_EXISTING_TOKEN')
        state = {'account': 'synthetic-account', 'calls': []}
        with self.public_google(account=state), \
             patch('finix.connections.subprocess.Popen', side_effect=AssertionError('Google must not spawn')) as spawn:
            self.act('start', provider='workspace', confirmed=True, local_browser=True)
            self.wait('verified')
            self.assertEqual(len(state['calls']), 1)
            self.act('apply')
            self.assertEqual(len(state['calls']), 2)
            spawn.assert_not_called()
        self.assertEqual(self.profile.config()['storage'], 'workspace')
        self.assertEqual(self.profile.config()['workspace_account'], hashlib.sha256(b'synthetic-account').hexdigest())
        self.assertEqual(token.read_text(), 'SYNTHETIC_EXISTING_TOKEN')
        self.assertIsNone(self.manager.sessions[self.profile.profile_id]['stage'])
        self.assertFalse((self.home / 'auth.json').exists())
        # Applied connection feeds the real profile backup/age/restore pipeline;
        # only the remote Drive transport is synthetic.
        from finix import core
        from finix.workspace_drive import WorkspaceDrive
        from tests.test_workspace_drive import FakeDrive
        identity = self.base / 'recovery.key'
        recipient = core.keygen(identity)
        self.profile.recipient(self.profile.profile_id, recipient, None)
        (self.home / 'config.yaml').write_text('synthetic: connected-backup\n')
        fake = FakeDrive()
        fake.account = 'synthetic-account'
        drive = WorkspaceDrive(self.home, self.profile.profile_id, self.profile.root / 'workspace',
                               runner=fake, account_digest=self.profile.config()['workspace_account'])
        with patch.object(Profile, 'workspace', return_value=drive):
            result = self.profile.backup()
        recovered = self.base / 'download.age'
        drive.download(result['workspace'], recovered)
        destination = self.base / 'restored'
        core.restore(recovered, identity, destination)
        self.assertEqual((destination / 'config.yaml').read_text(), 'synthetic: connected-backup\n')
        self.assertEqual(token.read_text(), 'SYNTHETIC_EXISTING_TOKEN')

    def test_workspace_new_login_requires_public_grant_before_spawn(self):
        before = self.profile.config()
        state = {'account': 'synthetic-account', 'calls': []}
        with self.public_google(signed_in=False, account=state), \
             patch('finix.connections.subprocess.Popen', side_effect=AssertionError('Google must not spawn')) as spawn:
            with self.assertRaisesRegex(ConnectionError, 'GOOGLE_AUTH_SETUP_REQUIRED'):
                self.act('start', provider='workspace', confirmed=True, local_browser=True)
            spawn.assert_not_called()
        self.assertEqual(state['calls'], [])
        self.assertEqual(self.manager.view(self.profile)['session']['status'], 'idle')
        self.assertEqual(self.profile.config(), before)
        for name in ('google_token.json', 'google_client_secret.json', 'auth.json'):
            self.assertFalse((self.home / name).exists())

    def test_workspace_apply_rechecks_grant_and_account_before_settings_write(self):
        before = self.profile.config()
        state = {'account': 'account-A', 'calls': []}
        with self.public_google(account=state):
            self.act('start', provider='workspace', confirmed=True, local_browser=True)
        session = self.manager.sessions[self.profile.profile_id]
        with self.public_google(signed_in=False), \
             patch('finix.connections.subprocess.Popen', side_effect=AssertionError('Google must not spawn')) as spawn:
            with self.assertRaisesRegex(ConnectionError, 'GOOGLE_AUTH_SETUP_REQUIRED'):
                self.act('apply')
            spawn.assert_not_called()
        state['account'] = 'account-B'
        with self.public_google(account=state):
            with self.assertRaisesRegex(ConnectionError, 'GOOGLE_ACCOUNT_CHANGED'):
                self.act('apply')
        self.assertEqual(session['public']['status'], 'verified')
        self.assertEqual(self.profile.config(), before)
        self.assertFalse((self.home / 'google_token.json').exists())
        self.assertFalse((self.home / 'auth.json').exists())
        self.assertEqual(self.profile.config()['storage'], 'local')

    def test_removed_workspace_worker_route_is_closed_without_secrets(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import Mock
        from finix import connection_worker
        output = io.StringIO()
        with patch('sys.argv', ['worker', 'workspace', str(self.home), str(self.base), '']), \
             patch('signal.alarm'), patch('signal.signal'), \
             patch('finix.connection_worker.connect_rclone', side_effect=AssertionError('removed Google route')) as connect, \
             redirect_stdout(output):
            self.assertEqual(connection_worker.main(), 1)
            connect.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {'status': 'failed', 'code': 'RCLONE_PROVIDER_FAILED'})
        self.assertNotIn('SYNTHETIC', output.getvalue())
        # Legacy closed remediation events still cross the reader safely, but
        # no Google token/client-config subprocess produces them anymore.
        for code in ('RCLONE_PROVIDER_FAILED', 'WORKSPACE_REFRESH_TOKEN_REQUIRED'):
            stage = self.base / ('failed-stage-' + code); stage.mkdir()
            event = json.dumps({'status': 'failed', 'code': code}) + '\n'
            process = Mock(stdout=io.BytesIO(event.encode()), stdin=io.BytesIO())
            process.wait.return_value = 1; process.poll.return_value = 1
            session = {'public': {'provider': 'workspace', 'status': 'working'}, 'process': process, 'stage': stage}
            self.manager._read(session)
            self.assertEqual(session['public']['status'], 'failed')
            self.assertEqual(session['public']['code'], code)
            self.assertFalse(stage.exists())

    def test_http_ticket_binding_and_reject_token_fields(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        path = Path(__file__).parents[1] / 'dashboard/plugin_api.py'
        spec = importlib.util.spec_from_file_location('finix_connection_api_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = FastAPI(); app.include_router(module.router)
        with patch.object(module, 'current_profile', return_value=self.profile), TestClient(app) as client:
            view = client.get('/connection')
            self.assertEqual(view.headers['cache-control'], 'no-store')
            view = view.json()
            body = dict(profile_id=view['profile_id'], ticket='bad', action='start', provider='yandex')
            self.assertEqual(client.post('/connection', json=body).status_code, 409)
            body['ticket'] = view['ticket']; body['token'] = 'NEVER_ACCEPT'
            self.assertEqual(client.post('/connection', json=body).status_code, 422)
            body.pop('token'); body['local_browser'] = False
            self.assertEqual(client.post('/connection', json=body).status_code, 409)


class FinixGoogleConnectionContracts(unittest.TestCase):
    def test_existing_grant_checked_without_skill_or_browser(self):
        from types import SimpleNamespace
        profile = SimpleNamespace(profile_id='a'*64, home=Path(os.environ['TMPDIR']), config=lambda: {'storage':'local'})
        manager = Connections()
        ticket = manager.view(profile)['ticket']
        with patch('finix.workspace_drive.WorkspaceRunner.__call__', return_value={'account_id':'mocked-provider'}):
            view = manager.action(profile, profile_id=profile.profile_id, ticket=ticket,
                                  action='start', provider='workspace', confirmed=True, local_browser=True)
        self.assertEqual(view['session']['status'], 'verified')
        self.assertNotIn('url', view['session'])
        self.assertEqual(profile.config(), {'storage':'local'})
        manager.action(profile, profile_id=profile.profile_id, ticket=ticket, action='cancel', session_id=view['session']['id'])
        self.assertEqual(manager.view(profile)['session']['status'],'cancelled')

    def test_failed_public_grant_check_is_closed_without_settings_mutation(self):
        from types import SimpleNamespace
        profile = SimpleNamespace(profile_id='a'*64, home=Path(os.environ['TMPDIR']), config=lambda: {'storage':'local'})
        manager = Connections()
        ticket = manager.view(profile)['ticket']
        with patch('finix.workspace_drive.WorkspaceRunner.__call__', side_effect=RuntimeError('PRIVATE_PROVIDER_SENTINEL')):
            with self.assertRaisesRegex(ConnectionError,'GOOGLE_AUTH_SETUP_REQUIRED') as failure:
                manager.action(profile, profile_id=profile.profile_id, ticket=ticket,
                               action='start', provider='workspace', confirmed=True, local_browser=True)
        self.assertNotIn('PRIVATE_PROVIDER_SENTINEL',str(failure.exception))
        self.assertEqual(manager.sessions,{})
        self.assertEqual(profile.config(),{'storage':'local'})

    def test_google_callback_urls_are_not_a_finix_authorization_route(self):
        self.assertFalse(safe_url('workspace','https://accounts.google.com/o/oauth2/v2/auth?response_type=code&state=x'))


class OneDriveStateContracts(unittest.TestCase):
    def test_supported_personal_choices_and_confirmation(self):
        import io
        from finix.connection_worker import connect_rclone
        steps = [
            {'State': 'oauth', 'Option': {'Name': 'config_is_local'}},
            {'State': 'choose_type_done', 'Option': {'Name': 'config_type', 'Examples': [{'Value': 'onedrive'}, {'Value': 'sharepoint'}]}},
            {'State': 'driveid_final', 'Option': {'Name': 'config_driveid', 'Examples': [{'Value': 'synthetic-drive', 'Help': 'Personal disk'}]}},
            {'State': 'driveid_final_end', 'Option': {'Name': 'config_drive_ok'}},
            {'State': ''}, {'IsDir': True}]
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp, \
             patch('finix.connection_worker.rclone_call', side_effect=[json.dumps(s).encode() for s in steps]) as call, \
             patch('finix.connection_worker.emit') as emit, patch('sys.stdin', io.StringIO('0\n')):
            connect_rclone('/synthetic/rclone', Path(tmp), 'onedrive')
            self.assertEqual(call.call_args_list[2].args[2][-1], 'onedrive')
            self.assertEqual(call.call_args_list[3].args[2][-1], 'synthetic-drive')
            self.assertEqual(call.call_args_list[4].args[2][-1], 'true')
            self.assertEqual(emit.call_args.kwargs, {'status': 'verified'})

    def test_remote_token_question_is_never_forwarded_or_accepted(self):
        from finix.connection_worker import connect_rclone
        data = {'State': 'remote', 'Option': {'Name': 'config_token', 'Help': 'SENTINEL_SECRET_HELP'}}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp, \
             patch('finix.connection_worker.rclone_call', return_value=json.dumps(data).encode()), \
             patch('finix.connection_worker.emit') as emit:
            with self.assertRaises(ValueError): connect_rclone('/synthetic/rclone', Path(tmp), 'onedrive')
            emit.assert_not_called()


class RealRcloneOAuthContracts(unittest.TestCase):
    def test_real_noninteractive_state_and_remote_prerequisite(self):
        binary = os.environ.get('FINIX_RCLONE_BINARY')
        if not binary:
            self.skipTest('FINIX_RCLONE_BINARY required for explicit isolated binary')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            os.chmod(tmp, 0o700)
            for kind in ('yandex', 'onedrive', 'dropbox'):
                path = Path(tmp) / (kind + '.conf')
                base = [binary, '--config', str(path), '--ask-password=false', 'config']
                p = subprocess.run([*base, 'create', 'finix', kind, '--non-interactive'], capture_output=True, timeout=5)
                self.assertEqual(p.returncode, 0)
                data = json.loads(p.stdout)
                self.assertEqual(data['Option']['Name'], 'config_is_local')
                p = subprocess.run([*base, 'update', 'finix', '--non-interactive', '--continue', '--state', data['State'], '--result', 'false'], capture_output=True, timeout=5)
                data = json.loads(p.stdout)
                self.assertEqual(data['Option']['Name'], 'config_token')
                self.assertIn('rclone authorize', data['Option']['Help'])
                self.assertNotIn('SYNTHETIC_TOKEN', p.stdout.decode())

    def test_real_local_rclone_callback_start_cancel_no_provider_requests(self):
        binary = os.environ.get('FINIX_RCLONE_BINARY')
        if not binary: self.skipTest('FINIX_RCLONE_BINARY required')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            home = Path(tmp) / 'profile'; home.mkdir(mode=0o700)
            profile = Profile(home); manager = Connections()
            with patch('finix.connections.shutil.which', return_value=binary):
                for kind in ('yandex', 'onedrive', 'dropbox'):
                    view = manager.view(profile)
                    try:
                        manager.action(profile, profile_id=profile.profile_id, ticket=view['ticket'], action='start', provider=kind, confirmed=True, local_browser=True)
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            view = manager.view(profile)
                            if view['session']['status'] in {'authorize', 'failed'}: break
                            time.sleep(.02)
                        self.assertEqual(view['session']['status'], 'authorize')
                        self.assertTrue(safe_url(kind, view['session']['url']))
                    finally:
                        s = manager.sessions.get(profile.profile_id)
                        if s: manager._stop(s, 'cancelled')
                    self.assertEqual(profile.config()['storage'], 'local')


if __name__ == '__main__': unittest.main()
