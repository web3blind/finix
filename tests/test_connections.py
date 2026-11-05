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
        self.env = patch.dict(os.environ, {'PATH': str(self.bin) + ':' + os.environ['PATH']})
        self.env.start()
        self.addCleanup(self.env.stop)
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

    def test_workspace_existing_profile_reuse_live_probe_no_token_copy(self):
        token = self.home / 'google_token.json'
        token.write_text('SYNTHETIC_EXISTING_TOKEN')
        helper = self.home / 'skills/productivity/google-workspace/scripts/google_api.py'
        helper.parent.mkdir(parents=True, mode=0o700)
        helper.write_text('''import os
from pathlib import Path
class Service:
 def about(self): return self
 def get(self, **kwargs): return self
 def execute(self):
  (Path(os.environ['HERMES_HOME']) / 'synthetic-probe').touch()
  return {'user': {}}
def build_service(*args): return Service()
''')
        self.act('start', provider='workspace', confirmed=True, local_browser=True)
        self.wait('verified')
        self.assertTrue((self.home / 'synthetic-probe').is_file())
        self.act('apply')
        self.assertEqual(self.profile.config()['storage'], 'workspace')
        self.assertEqual(token.read_text(), 'SYNTHETIC_EXISTING_TOKEN')
        self.assertFalse((self.manager.sessions[self.profile.profile_id]['stage'] / 'workspace-token.json').exists())

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


class WorkspaceOAuthContracts(unittest.TestCase):
    def test_google_state_mismatch_fails_before_token_request(self):
        from google_auth_oauthlib.flow import InstalledAppFlow
        from oauthlib.oauth2 import MismatchingStateError
        config = {'installed': {'client_id': 'synthetic', 'client_secret': 'synthetic',
                                 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                                 'token_uri': 'https://oauth2.googleapis.com/token'}}
        flow = InstalledAppFlow.from_client_config(config, ['https://www.googleapis.com/auth/drive'])
        flow.redirect_uri = 'http://127.0.0.1:12345/'
        flow.authorization_url()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('must reject before network')):
            with self.assertRaises(MismatchingStateError):
                flow.fetch_token(authorization_response='https://127.0.0.1:12345/?state=wrong&code=SYNTHETIC_GRANT')

    def test_real_google_library_loopback_callback_exchange_and_private_staging(self):
        import contextlib
        import io
        import threading
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from urllib.parse import parse_qs, urlsplit
        from unittest.mock import Mock
        from google_auth_oauthlib.flow import InstalledAppFlow
        from finix.connection_worker import connect_workspace

        seen = []
        class Issuer(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                seen.append(payload.get('code') == ['SYNTHETIC_GRANT'])
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'access_token': 'SYNTHETIC_ACCESS', 'refresh_token': 'SYNTHETIC_REFRESH',
                                            'token_type': 'Bearer', 'expires_in': 3600,
                                            'scope': 'https://www.googleapis.com/auth/drive'}).encode())
        server = HTTPServer(('127.0.0.1', 0), Issuer)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                home = Path(tmp) / 'profile'; home.mkdir(mode=0o700)
                stage = Path(tmp) / 'stage'; stage.mkdir(mode=0o700)
                payload = {'installed': {'client_id': 'synthetic-client', 'client_secret': 'SYNTHETIC_CLIENT_SECRET',
                                         'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                                         'token_uri': 'https://oauth2.googleapis.com/token',
                                         'redirect_uris': ['http://localhost']}}
                (home / 'google_client_secret.json').write_text(json.dumps(payload))
                original = InstalledAppFlow.from_client_config
                def fixture(config, scopes):
                    config = json.loads(json.dumps(config))
                    config['installed']['token_uri'] = 'http://127.0.0.1:%d/token' % server.server_port
                    return original(config, scopes)
                output, errors = io.StringIO(), []
                def run():
                    try: connect_workspace(home, stage)
                    except BaseException as exc: errors.append(type(exc).__name__)
                with patch.dict(os.environ, {'OAUTHLIB_INSECURE_TRANSPORT': '1'}), \
                     patch.object(InstalledAppFlow, 'from_client_config', side_effect=fixture), \
                     patch('googleapiclient.discovery.build', return_value=Mock()), \
                     contextlib.redirect_stdout(output):
                    worker = threading.Thread(target=run, daemon=True); worker.start()
                    deadline = time.monotonic() + 5
                    while 'FINIX_AUTH ' not in output.getvalue() and time.monotonic() < deadline: time.sleep(.01)
                    url = output.getvalue().split('FINIX_AUTH ', 1)[1].splitlines()[0]
                    query = parse_qs(urlsplit(url).query)
                    callback = query['redirect_uri'][0] + '?state=' + query['state'][0] + '&code=SYNTHETIC_GRANT'
                    with urlopen(callback, timeout=5) as response: self.assertEqual(response.status, 200)
                    worker.join(8)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(seen, [True])
                token = stage / 'workspace-token.json'
                self.assertEqual(token.stat().st_mode & 0o777, 0o600)
                self.assertFalse((home / 'google_token.json').exists())
                for secret in ('SYNTHETIC_ACCESS', 'SYNTHETIC_REFRESH', 'SYNTHETIC_GRANT', 'SYNTHETIC_CLIENT_SECRET'):
                    self.assertNotIn(secret, output.getvalue())
        finally:
            server.shutdown(); server.server_close(); thread.join()


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
