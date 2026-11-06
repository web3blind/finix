"""Real PTB dispatch and real Hermes SDK, with synthetic Bot API transport only."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ExtBot, MessageHandler, filters
from telegram.request import BaseRequest

from finix import core
from finix.integration import Profile, register


class Transport(BaseRequest):
    def __init__(self):
        self.calls = []
        self.serial = 100

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit('/', 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == 'getMe':
            result = {'id': 999, 'is_bot': True, 'first_name': 'Synthetic', 'username': 'synthetic_bot'}
        elif name == 'answerCallbackQuery':
            result = True
        else:
            self.serial += 1
            result = {'message_id': self.serial, 'date': 1, 'chat': {'id': int(params['chat_id']), 'type': 'private'},
                      'from': {'id': 999, 'is_bot': True, 'first_name': 'Synthetic'}, 'text': params.get('text', '')}
        return 200, json.dumps({'ok': True, 'result': result}).encode()


class TelegramMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'profile'
        self.home.mkdir(mode=0o700)
        (self.home / 'config.yaml').write_text('test: synthetic\n')
        self.recipient = core.keygen(Path(self.tmp.name) / 'identity')
        self.profile = Profile(self.home)
        self.manager = PluginManager(scope_key=str(self.home))
        ctx = PluginContext(PluginManifest(name='finix', path=str(Path(__file__).parents[1])), self.manager)
        self.leases = []
        on_unload = ctx.on_unload
        def capture(fn):
            lease = on_unload(fn)
            self.leases.append(lease)
            return lease
        token = set_hermes_home_override(self.home)
        try:
            with patch.object(ctx, 'get_config', side_effect=lambda key, default='':
                              {'owner_id': '42', 'owner_platform': 'telegram'}.get(key, default)), \
                 patch.object(ctx, 'on_unload', side_effect=capture):
                register(ctx)
        finally:
            reset_hermes_home_override(token)
        # Keep auth attestation synthetic without replacing the registered menu.
        self.factory = self.manager._platform_handler_factories['telegram'][0][0]
        self.menu = self.factory.__self__
        self.menu.owner = lambda: ('42', 'telegram')
        self.transport = Transport()
        bot = ExtBot('123:synthetic', request=self.transport, get_updates_request=Transport())
        self.app = Application.builder().bot(bot).build()
        self.factory(self.app, object())
        self.core = []
        self.errors = []
        async def core_handler(update, context):
            self.core.append(update.update_id)
        async def error(update, context):
            self.errors.append(context.error)
        self.app.add_handler(CallbackQueryHandler(core_handler))
        self.app.add_handler(MessageHandler(filters.ALL, core_handler))
        self.app.add_error_handler(error)
        await self.app.initialize()
        self.sequence = 0

    async def asyncTearDown(self):
        for lease in self.leases:
            lease.dispose()
        await self.app.shutdown()
        self.assertEqual(self.errors, [])

    async def send(self, text='/finix', user=42, chat=42, kind='private', thread=None):
        self.sequence += 1
        message = {'message_id': self.sequence, 'date': 1, 'chat': {'id': chat, 'type': kind},
                   'from': {'id': user, 'is_bot': False, 'first_name': 'Owner'}, 'text': text}
        if thread:
            message['message_thread_id'] = thread
        if text.startswith('/'):
            message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
        await self.app.process_update(Update.de_json({'update_id': self.sequence, 'message': message}, self.app.bot))

    def latest(self):
        return [params for name, params in self.transport.calls if name == 'sendMessage'][-1]

    def ticket(self, label):
        markup = self.latest()['reply_markup']
        if isinstance(markup, str):
            markup = json.loads(markup)
        for row in markup['inline_keyboard']:
            for button in row:
                if label in button['text']:
                    return button['callback_data']
        raise AssertionError((label, markup))

    async def click(self, label=None, data=None, user=42, message_id=None, chat=42, thread=None):
        data = data if data is not None else self.ticket(label)
        self.sequence += 1
        session = self.menu.sessions.get((chat, thread))
        message = {'message_id': message_id if message_id is not None else session.message_id,
                   'date': 1, 'chat': {'id': chat, 'type': 'private'},
                   'from': {'id': 999, 'is_bot': True, 'first_name': 'Bot'}, 'text': 'menu'}
        if thread:
            message['message_thread_id'] = thread
        query = {'id': str(self.sequence), 'from': {'id': user, 'is_bot': False, 'first_name': 'Owner'},
                 'chat_instance': 'synthetic', 'data': data, 'message': message}
        await self.app.process_update(Update.de_json({'update_id': self.sequence, 'callback_query': query}, self.app.bot))
        return data

    async def test_long_backup_renews_session_before_period_prompt(self):
        import time
        self.profile.recipient(self.profile.profile_id, self.recipient, None)
        await self.send()
        await self.click('Только локально')
        session = self.menu.sessions[(42, None)]
        check = session.flow.check_and_run

        def finish_after_deadline():
            result = check()
            session.deadline = time.monotonic() - 1
            return result

        with patch.object(session.flow, 'check_and_run', side_effect=finish_after_deadline):
            await self.click('Проверить и создать')
        self.assertIn('Укажите период', self.latest()['text'])
        self.assertGreater(session.deadline, time.monotonic())
        await self.click('24h')
        self.assertEqual(self.profile.config()['interval_hours'], 24)
        self.assertFalse(self.core)

    async def test_real_sdk_dispatch_key_local_backup_strict_period_and_unload(self):
        self.assertIn('finix', self.manager._cli_commands)
        await self.send()
        await self.click('Скачать')
        self.assertTrue(any(name == 'sendDocument' for name, _ in self.transport.calls))
        await self.click('Ввести')
        await self.send('not-a-public-recipient')
        self.assertIsNone(self.profile.config()['recipient'])
        self.assertFalse(self.core)
        await self.send(self.recipient)
        self.assertEqual(self.profile.config()['recipient'], self.recipient)
        self.assertIn('замена заблокирована', self.latest()['text'])
        await self.click('Только локально')
        await self.click('Проверить и создать')
        self.assertTrue(list((self.profile.root / 'copies').glob('*.age')))
        self.assertIn('24h', self.latest()['text'])
        await self.send('every day')
        self.assertEqual(self.profile.config()['interval_hours'], 0)
        await self.send('7d')
        self.assertEqual(self.profile.config()['interval_hours'], 168)
        await self.send('unrelated message')
        self.assertEqual(len(self.core), 1)
        for lease in self.leases:
            lease.dispose()
        self.assertFalse(self.menu.alive)
        self.assertFalse(self.menu.sessions)
        self.assertNotIn(-10, self.app.handlers)

    async def test_owner_group_replay_wrong_message_expiry_and_core_callbacks(self):
        await self.send(user=7)
        self.assertFalse(self.menu.sessions)
        await self.send(chat=-4, kind='group')
        self.assertFalse(self.menu.sessions)
        await self.send()
        ticket = self.ticket('Ввести')
        await self.click(data=ticket, user=7)
        self.assertFalse(self.menu.sessions[(42, None)].pending)
        await self.click(data=ticket, message_id=99999)
        self.assertFalse(self.menu.sessions[(42, None)].pending)
        await self.click(data=ticket)
        self.assertEqual(self.menu.sessions[(42, None)].pending, 'recipient')
        await self.click(data=ticket)
        self.assertEqual(self.menu.sessions[(42, None)].pending, 'recipient')
        await self.click('Отмена')
        await self.send('ordinary input')
        await self.send()
        ticket = self.ticket('Ввести')
        self.menu.sessions[(42, None)].deadline = time.monotonic() - 1
        await self.click(data=ticket, message_id=self.transport.serial)
        self.assertFalse(self.menu.sessions)
        await self.click(data='other:callback', message_id=123)
        self.assertEqual(len(self.core), 2)

    async def test_cloud_modes_status_choice_apply_and_period_button(self):
        self.profile.recipient(self.profile.profile_id, self.recipient, None)
        calls = []
        class Commands:
            def execute(inner, profile, args, actor):
                calls.append((args, actor))
                if args[0] in {'workspace', 'yandex', 'onedrive', 'dropbox'}:
                    result = {'id': 'synthetic', 'provider': args[0], 'status': 'authorize',
                              'url': 'http://127.0.0.1:53682/auth?state=synthetic', 'instructions': 'SSH instructions'}
                elif args[0] == 'status':
                    result = {'id': 'synthetic', 'provider': 'onedrive', 'status': 'choice',
                              'choices': [{'id': '0', 'label': 'Synthetic account'}]}
                elif args[0] == 'choose':
                    result = {'id': 'synthetic', 'provider': 'onedrive', 'status': 'verified'}
                else:
                    result = {'id': 'synthetic', 'provider': 'onedrive', 'status': 'applied'}
                return json.dumps(result)
        self.menu.connections = Commands()
        await self.send()
        for provider in ('Google Workspace', 'Яндекс Диск', 'OneDrive', 'Dropbox'):
            self.assertTrue(self.ticket(provider))
        await self.click('OneDrive')
        self.assertFalse(calls)
        await self.click('SSH-туннель')
        self.assertEqual(calls[0], (['onedrive', 'tunnel', 'confirm'], ('telegram', '42')))
        self.assertIn('http://127.0.0.1:53682/auth?state=synthetic', self.latest()['text'])
        await self.click('Обновить')
        await self.click('Synthetic account')
        self.assertEqual(calls[-1][0], ['choose', 'synthetic', '0'])
        await self.click('Применить')
        self.assertEqual(calls[-1][0], ['apply', 'synthetic'])
        await self.click('Только локально')
        await self.click('Проверить и создать')
        await self.click('24h')
        self.assertEqual(self.profile.config()['interval_hours'], 24)

    async def test_pending_cancel_returns_to_core_and_backup_runs_off_loop_once(self):
        import threading
        from finix.setup_flow import SetupFlow
        await self.send()
        await self.click('Ввести')
        await self.send('/cancel')
        await self.send('ordinary text')
        self.assertFalse(self.menu.sessions)
        self.assertEqual(len(self.core), 2)
        self.profile.recipient(self.profile.profile_id, self.recipient, None)
        await self.send()
        await self.click('Только локально')
        ticket = self.ticket('Проверить и создать')
        original = SetupFlow.check_and_run
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow(flow):
            calls.append(threading.get_ident())
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test timeout')
            return original(flow)
        with patch.object(SetupFlow, 'check_and_run', slow):
            task = asyncio.create_task(self.click(data=ticket))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertTrue(self.menu.sessions[(42, None)].busy)
                await self.click(data=ticket)
                self.assertEqual(len(calls), 1)
                self.assertNotEqual(calls[0], threading.get_ident())
            finally:
                release.set()
                await task
        self.assertTrue(self.menu.sessions[(42, None)].pending == 'period')

    async def test_profile_changed_and_thread_context_cannot_mutate(self):
        await self.send()
        ticket = self.ticket('Ввести')
        await self.click(data=ticket, message_id=self.transport.serial, thread=88)
        self.assertFalse(self.menu.sessions[(42, None)].pending)
        other_home = Path(self.tmp.name) / 'other'
        other_home.mkdir()
        other = Profile(other_home)
        self.menu.profile_factory = lambda: other
        await self.click(data=ticket)
        self.assertFalse(self.menu.sessions)
        self.assertIsNone(other.config()['recipient'])
        self.assertIsNone(self.profile.config()['recipient'])
