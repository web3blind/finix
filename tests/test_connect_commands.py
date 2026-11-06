"""Owner chat and standalone connect; synthetic grants only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tests.test_connections as fixtures
from finix.connections import Connections
from finix.integration import Profile
from finix.integration import command


class ChatConnectTests(unittest.TestCase):
    manager: Connections
    profile: Profile
    bin: Path
    base: Path
    home: Path
    stop = fixtures.ConnectionsTests.stop
    wait = fixtures.ConnectionsTests.wait

    def test_native_async_handler_profile_binding_choice_and_unload(self):
        import asyncio
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from finix.integration import register
        # Drive selection is emitted by the real credential-owning worker.
        fake = fixtures.FAKE_RCLONE.replace("elif 'update' in args:", """elif 'update' in args and args[args.index('--state')+1]=='pick':
 print(json.dumps({'State':''}))
elif 'update' in args:""").replace("print(json.dumps({'State':''}))\nelif 'lsjson'", """print(json.dumps({'State':'pick','Option':{'Name':'config_driveid','Examples':[{'Value':'synthetic','Help':'Личный диск'}]}}))
elif 'lsjson'""")
        (self.bin / 'rclone').write_text(fake)
        manager = PluginManager(scope_key=str(self.home))
        ctx = PluginContext(PluginManifest(name='finix', path=str(Path(__file__).parents[1])), manager)
        callbacks = []
        on_unload = ctx.on_unload
        def capture(callback):
            lease = on_unload(callback); callbacks.append(lease); return lease
        token = set_hermes_home_override(self.home)
        try:
            with patch.object(ctx, 'get_config', side_effect=lambda key, default='': {'owner_id':'owner','owner_platform':'telegram'}.get(key, default)), \
                 patch.object(ctx, 'on_unload', side_effect=capture):
                register(ctx)
                handler = manager._plugin_commands['finix']['handler']
                async def run():
                    source = SimpleNamespace(user_id='owner', chat_type='dm')
                    async def call(text): return await handler(text, source=source, platform='telegram')
                    ident = json.loads(await call('connect onedrive server-browser confirm'))['id']
                    event = {}
                    deadline = time.monotonic()+30
                    while time.monotonic()<deadline:
                        event = json.loads(await call('connect status '+ident))
                        if event['status']=='authorize': break
                        await asyncio.sleep(.02)
                    self.assertEqual(event['status'], 'authorize')
                    from urllib.request import urlopen
                    def callback():
                        with urlopen(event['url']+'&code=SYNTHETIC_GRANT',timeout=5): pass
                    await asyncio.to_thread(callback)
                    while time.monotonic()<deadline:
                        event = json.loads(await call('connect status '+ident))
                        if event['status']=='choice': break
                        await asyncio.sleep(.02)
                    self.assertEqual(event['choices'], [{'id':'0','label':'Личный диск'}])
                    self.assertIn('INVALID_CHOICE',await call('connect choose '+ident+' 9'))
                    self.assertEqual(json.loads(await call('connect choose '+ident+' 0'))['status'],'working')
                    while time.monotonic()<deadline:
                        event = json.loads(await call('connect status '+ident))
                        if event['status']=='verified': break
                        await asyncio.sleep(.02)
                    self.assertEqual(event['status'],'verified')
                    self.assertEqual(self.profile.config()['storage'],'local')
                    for lease in callbacks: lease.dispose()
                    self.assertIn('PLUGIN_UNLOADED',await call('connect apply '+ident))
                asyncio.run(run())
        finally:
            for lease in callbacks: lease.dispose()
            reset_hermes_home_override(token)

    def setUp(self):
        fixtures.ConnectionsTests.setUp(self)
        from finix.connect_commands import ConnectCommands
        self.chat = ConnectCommands(self.manager)
        self.addCleanup(self.chat.close)

    def call(self, text, actor='owner', profile=None):
        with patch('finix.integration.current_profile', return_value=profile or self.profile):
            return command(text, owner_id='owner', owner_platform='telegram', platform='telegram',
                           source=SimpleNamespace(user_id=actor, chat_type='dm'), connection_commands=self.chat)

    def test_chat_callable_flow_deny_replay_and_secret_fields(self):
        self.assertIn('denied', self.call('connect yandex tunnel confirm', actor='foreign'))
        self.assertEqual(self.manager.view(self.profile)['session']['status'], 'idle')
        result = json.loads(self.call('connect yandex tunnel confirm'))
        ident = result['id']
        view = self.wait('authorize')
        public = self.call('connect status ' + ident)
        self.assertIn('ssh -N -L 127.0.0.1:53682:127.0.0.1:53682', public)
        self.assertNotIn(view['ticket'], public)
        self.assertNotIn('SYNTHETIC_TOKEN', public)
        for bad in ('connect apply wrong', 'connect status wrong', 'connect code SECRET',
                    'connect yandex tunnel confirm token=SECRET'):
            self.assertIn('Finix connect:', self.call(bad))
        self.assertEqual(self.profile.config()['storage'], 'local')
        from urllib.request import urlopen
        with urlopen(view['session']['url'] + '&code=SYNTHETIC_GRANT', timeout=5): pass
        self.wait('verified')
        self.assertEqual(json.loads(self.call('connect apply ' + ident))['status'], 'applied')
        self.assertEqual(self.profile.config()['storage'], 'rclone')
        self.assertIn('Finix connect:', self.call('connect apply ' + ident))

    def test_profile_owner_unload_and_consent(self):
        from finix.integration import Profile
        for text in ('connect yandex tunnel', 'connect yandex invalid confirm'):
            self.assertIn('Finix connect:', self.call(text))
        ident = json.loads(self.call('connect yandex server-browser confirm'))['id']
        self.wait('authorize')
        other = Profile(self.base / 'other')
        self.assertIn('Finix connect:', self.call('connect cancel ' + ident, profile=other))
        self.chat.close()
        self.assertEqual(self.manager.view(self.profile)['session']['status'], 'cancelled')
        self.assertIn('Finix connect:', self.call('connect yandex local confirm'))

    def test_cancel_replay_concurrent_start_and_private_chat_guard(self):
        from concurrent.futures import ThreadPoolExecutor
        # Controller locking fences concurrent commands before the callback bind.
        def start():
            try:
                return self.chat.execute(self.profile, ['yandex','local','confirm'], ('telegram','owner'))
            except Exception as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(sum('CONNECTION_BUSY' in value for value in results), 1)
        ident = self.manager.view(self.profile)['session']['id']
        self.wait('authorize')
        self.assertEqual(json.loads(self.call('connect cancel '+ident))['status'], 'cancelled')
        self.assertEqual(self.profile.config()['storage'], 'local')
        self.assertIn('INVALID_TRANSITION', self.call('connect apply '+ident))
        with patch('finix.integration.current_profile',return_value=self.profile):
            denied = command('connect yandex tunnel confirm',owner_id='owner',owner_platform='telegram',
                             platform='telegram',source=SimpleNamespace(user_id='owner',chat_type='group'))
        self.assertIn('PRIVATE_CHAT_REQUIRED',denied)

    def test_google_start_url_filter_rejects_secrets_callbacks_and_controls(self):
        from urllib.parse import urlencode
        from finix.connections import safe_url
        from finix.connect_commands import public_session
        params = dict(response_type='code',client_id='fixture',redirect_uri='http://127.0.0.1:53683/',state='fixture')
        url = 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params)
        self.assertTrue(safe_url('workspace',url))
        for extra in ('&code=SECRET','&client_secret=SECRET','&code_verifier=SECRET','&state=duplicate','\n'):
            self.assertFalse(safe_url('workspace',url+extra))
        result = public_session({'session':{'provider':'workspace','status':'authorize','url':url}},'tunnel')
        self.assertIn('127.0.0.1:53683:127.0.0.1:53683',result['instructions'])

    def test_cli_refuses_no_consent_and_server_browser_autostart_in_tunnel(self):
        from finix.connect_cli import interactive
        from finix.connections import ConnectionError
        for options in ({'confirm':False},{'confirm':True,'open_browser':True}):
            with self.assertRaises(ConnectionError):
                interactive('yandex',str(self.home),'tunnel',**options)
        self.assertEqual(self.manager.view(self.profile)['session']['status'],'idle')

    def test_standalone_interactive_real_subprocess_callback_and_apply(self):
        import selectors
        from urllib.request import urlopen
        # Provider transport only is synthetic; the rest of the flow is real.
        fake = fixtures.FAKE_RCLONE.replace("elif 'lsjson' in args: print('{\"IsDir\":true}')", '''elif 'listremotes' in args:
 print('[{"name":"finix","type":"yandex","source":"file"}]')
elif 'copyto' in args:
 import shutil
 i=args.index('copyto'); target=Path(__file__).parent/'cloud'/args[i+2].split(':',1)[1]
 target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(args[i+1],target)
elif 'cat' in args:
 target=Path(__file__).parent/'cloud'/args[args.index('cat')+1].split(':',1)[1]
 sys.stdout.buffer.write(target.read_bytes())
elif 'lsjson' in args:
 value=args[args.index('lsjson')+1].split(':',1)[1]
 if not value: print('{"IsDir":true}')
 else:
  target=Path(__file__).parent/'cloud'/value
  print(json.dumps({'IsDir':False,'Size':target.stat().st_size,'Name':target.name}))''')
        (self.bin / 'rclone').write_text(fake)
        cli_python = os.environ.get('FINIX_CLI_PYTHON', sys.executable)
        cli_cwd = self.base if os.environ.get('FINIX_CLI_PYTHON') else None
        p = subprocess.Popen([cli_python, '-m', 'finix', 'connect', 'yandex', '--home', str(self.home),
                              '--mode', 'tunnel', '--confirm'], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cli_cwd)
        assert p.stdin is not None and p.stdout is not None and p.stderr is not None
        lines = []
        try:
            deadline = time.monotonic() + 30
            with selectors.DefaultSelector() as sel:
                sel.register(p.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not sel.select(.2): continue
                    line = p.stdout.readline()
                    if not line: break
                    lines.append(line)
                    event = json.loads(line)
                    if event.get('status') == 'authorize':
                        with urlopen(event['url'] + '&code=SYNTHETIC_GRANT', timeout=5): pass
                    if event.get('status') == 'verified':
                        p.stdin.write('apply\n'); p.stdin.flush()
                    if event.get('status') == 'applied': break
            p.stdin.close()
            self.assertEqual(p.wait(timeout=5), 0, ''.join(lines))
            self.assertEqual(self.profile.config()['storage'], 'rclone')
            self.assertIn('"status": "applied"', ''.join(lines))
            for secret in ('SYNTHETIC_TOKEN', 'SYNTHETIC_GRANT', 'ticket'):
                self.assertNotIn(secret, ''.join(lines))
            from finix import core
            identity = self.base / 'recovery.key'
            self.profile.recipient(self.profile.profile_id, core.keygen(identity), None)
            (self.home / 'config.yaml').write_text('synthetic: cli-connect-roundtrip\n')
            (self.home / 'cron').mkdir()
            (self.home / 'cron/jobs.json').write_text('{"jobs":[{"id":"fixture","enabled":true}]}')
            result = json.loads(self.call('backup'))
            recovered = self.base / 'download.age'
            self.profile.rclone().download(result['rclone'], recovered)
            destination = self.base / 'restored'
            subprocess.run([cli_python, '-m', 'finix', 'restore', '--archive', str(recovered),
                            '--identity', str(identity), '--target', str(destination)],
                           check=True, capture_output=True, timeout=10, cwd=cli_cwd)
            self.assertEqual((destination / 'config.yaml').read_text(), 'synthetic: cli-connect-roundtrip\n')
            self.assertFalse(json.loads((destination / 'cron/jobs.json').read_text())['jobs'][0]['enabled'])
        finally:
            if p.poll() is None: p.kill(); p.wait()
            p.stdout.close(); p.stderr.close()
            if not p.stdin.closed: p.stdin.close()
