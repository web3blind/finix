"""Native Telegram setup UI; opaque one-use buttons never carry account data."""
import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import time
from types import SimpleNamespace

from . import core
from .connections import PROVIDERS, safe_url

TTL = 300
GROUP = -10


from .assets import key_page


@dataclass
class Session:
    binding: tuple
    profile: object
    flow: object
    deadline: float
    message_id: int | None = None
    pending: str = ''
    busy: bool = False
    tickets: dict = field(default_factory=dict)
    connection: str = ''
    local_selected: bool = False


class TelegramMenu:
    def __init__(self, profile_factory, connection_commands, owner, legacy):
        self.profile_factory = profile_factory
        self.connections = connection_commands
        self.owner = owner
        self.legacy = legacy
        self.sessions = {}
        self.alive = True
        self.installed = []

    def close(self):
        self.alive = False
        self.sessions.clear()
        for app, handler in self.installed:
            app.remove_handler(handler, group=GROUP)
        self.installed.clear()

    def factory(self, native, adapter):
        if not self.alive:
            return
        from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters
        menu = self

        class Pending(filters.MessageFilter):
            def filter(self, message):
                session = menu.session_for(message)
                return bool(session and session.pending)

        handlers = [CommandHandler('finix', self.command),
                    CallbackQueryHandler(self.callback, pattern=r'^finix:'),
                    MessageHandler(filters.COMMAND & Pending(), self.clear_pending),
                    MessageHandler(filters.TEXT & ~filters.COMMAND & Pending(), self.input)]
        for handler in handlers:
            native.add_handler(handler, group=GROUP)
            self.installed.append((native, handler))

    def allowed(self, message, user_id=None):
        owner_id, platform = self.owner()
        return (self.alive and platform == 'telegram' and bool(owner_id)
                and str(user_id if user_id is not None else message.from_user.id) == str(owner_id)
                and message.chat.type == 'private')

    @staticmethod
    def context(message):
        return (message.chat.id, message.message_thread_id)

    def session_for(self, message, user_id=None):
        if not self.allowed(message, user_id):
            return None
        session = self.sessions.get(self.context(message))
        actor = str(user_id if user_id is not None else message.from_user.id)
        if session and (session.busy or session.deadline > time.monotonic()) and session.binding[0] == actor:
            return session
        self.sessions.pop(self.context(message), None)
        return None

    async def command(self, update, context):
        from telegram.ext import ApplicationHandlerStop
        message = update.effective_message
        if not self.allowed(message):
            await message.reply_text('Finix: open /finix in the bot owner’s private chat.')
            raise ApplicationHandlerStop
        if context.args and context.args != ['setup']:
            self.sessions.pop(self.context(message), None)
            result = await self.legacy(' '.join(context.args), platform='telegram',
                                       source=SimpleNamespace(user_id=update.effective_user.id, chat_type='dm'))
            await message.reply_text(result)
            raise ApplicationHandlerStop
        old = self.session_for(message)
        if old and old.busy:
            await message.reply_text('Finix: a check is already running.')
            raise ApplicationHandlerStop
        from .setup_flow import SetupFlow
        profile = await asyncio.to_thread(self.profile_factory)
        if not self.alive:
            raise ApplicationHandlerStop
        session = Session((str(update.effective_user.id), *self.context(message), profile.profile_id),
                          profile, SetupFlow(profile), time.monotonic() + TTL)
        self.sessions[self.context(message)] = session
        await self.render(message, session)
        raise ApplicationHandlerStop

    def button(self, session, label, action, value=''):
        from telegram import InlineKeyboardButton
        ticket = secrets.token_urlsafe(12)
        session.tickets[ticket] = (action, value)
        return InlineKeyboardButton(label, callback_data='finix:' + ticket)

    async def display(self, message, session, text, rows):
        from telegram import InlineKeyboardMarkup
        sent = await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows) if rows else None,
                                        disable_web_page_preview=True)
        session.message_id = sent.message_id

    async def render(self, message, session):
        if not self.alive or self.sessions.get(self.context(message)) is not session:
            return
        session.tickets.clear()
        session.pending = ''
        view = await asyncio.to_thread(session.flow.view)
        if not self.alive or self.sessions.get(self.context(message)) is not session:
            return
        # Long backup/authorization I/O must leave a fresh owner-bound input window.
        session.deadline = time.monotonic() + TTL
        button = lambda label, action, value='': self.button(session, label, action, value)
        stage = view['stage']
        if not view['recipient_present']:
            text = ('1/3. Create a key on your device using the offline page. Store the private key separately. '
                    'Only the public age1 address is allowed in chat. Never send a private key, codes or tokens.')
            rows = [[button('Download offline page', 'keypage')], [button('Enter public age1', 'recipient')]]
        elif stage == 'cloud' or (view['storage'] == 'local' and not session.local_selected and not view['verified']):
            text = '1/3. The key is already registered; replacement is locked.\n2/3. Choose cloud storage.'
            rows = [[button(label, 'provider', provider)] for provider, label in PROVIDERS.items()]
            rows.append([button('Local only (not cloud)', 'local')])
        elif stage in {'check', 'schedule'}:
            text = '3/3. Check settings, create the first backup and verify cloud readback.'
            rows = [[button('Check and create backup', 'check')]]
            if view['verified']:
                text = '3/3. Backup verified. Enter the interval exactly as 24h or 7d.'
                session.pending = 'period'
                rows = [[button('24h', 'period', '24h'), button('7d', 'period', '7d')]]
        else:
            text = 'Finix is configured. Interval: ' + str(view['interval_hours']) + ' hours. The key is locked.'
            rows = [[button('Verify a new backup', 'check')]]
        rows.append([button('Cancel', 'cancel')])
        await self.display(message, session, text, rows)

    async def callback(self, update, context):
        from telegram.ext import ApplicationHandlerStop
        query = update.callback_query
        await query.answer()
        message = query.message
        session = self.session_for(message, query.from_user.id) if message else None
        if (not session or session.busy or message.message_id != session.message_id
                or not isinstance(query.data, str)):
            raise ApplicationHandlerStop
        action = session.tickets.pop(query.data.removeprefix('finix:'), None)
        if action is None:
            raise ApplicationHandlerStop
        session.tickets.clear()
        session.pending = ''
        session.busy = True
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            current = await asyncio.to_thread(self.profile_factory)
            if not self.alive or current.profile_id != session.binding[-1]:
                self.sessions.pop(self.context(message), None)
                raise ApplicationHandlerStop
            await self.act(message, session, *action)
        except (core.FinixError, ValueError, OSError, RuntimeError):
            if self.alive:
                await self.display(message, session, 'Finix: operation failed. Check local setup; use /finix to retry.', [])
        finally:
            session.busy = False
        raise ApplicationHandlerStop

    async def act(self, message, session, action, value):
        button = lambda label, action, value='': self.button(session, label, action, value)
        if action == 'cancel':
            if session.connection:
                await self.connect(session, ['cancel', session.connection])
            self.sessions.pop(self.context(message), None)
            await message.reply_text('Finix: cancelled.')
        elif action == 'keypage':
            path = key_page()
            with path.open('rb') as document:
                await message.reply_document(document, filename='finix-keygen.html',
                                             caption='Download and open locally. The private key stays with you.')
            await self.render(message, session)
        elif action == 'recipient':
            if session.profile.config().get('recipient'):
                await self.render(message, session)
                return
            session.pending = 'recipient'
            await self.display(message, session, 'Send only your public age1 address in the next message.', [[button('Cancel', 'cancel')]])
        elif action == 'local':
            await asyncio.to_thread(session.profile.configure, 'storage', 'local')
            session.local_selected = True
            await self.render(message, session)
        elif action == 'provider':
            text = ('Choose a sign-in method. Confirm the provider’s requested permissions. '
                    'Google uses Finix OAuth: run hermes auth add finix:google in this profile first. Enter passwords only on the provider site; '
                    'never send codes, callback URLs or tokens in chat. No browser opens automatically.')
            rows = [[button(label, 'start', (value, mode))] for label, mode in
                    [('On the Finix machine', 'local'), ('SSH tunnel', 'tunnel'), ('Browser on server', 'server-browser')]]
            await self.display(message, session, text, rows + [[button('Cancel', 'cancel')]])
        elif action == 'start':
            provider, mode = value
            view = await self.connect(session, [provider, mode, 'confirm'])
            session.connection = view['id']
            await self.connection_view(message, session, view)
        elif action in {'status', 'choose', 'apply'}:
            args = [action, session.connection] + ([value] if action == 'choose' else [])
            view = await self.connect(session, args)
            if action == 'apply':
                await self.render(message, session)
            else:
                await self.connection_view(message, session, view)
        elif action == 'check':
            await message.reply_text('Finix: checking and creating a backup…')
            await asyncio.to_thread(session.flow.check_and_run)
            if self.alive:
                await self.render(message, session)
        elif action == 'period':
            await asyncio.to_thread(session.flow.schedule, value)
            if self.alive:
                await self.render(message, session)

    async def connect(self, session, args):
        return json.loads(await asyncio.to_thread(self.connections.execute, session.profile, args,
                                                  ('telegram', session.binding[0])))

    async def connection_view(self, message, session, view):
        button = lambda label, action, value='': self.button(session, label, action, value)
        text = 'Cloud: ' + view['status'] + '\n' + view.get('instructions', '')
        url = view.get('url')
        if url and safe_url(view['provider'], url):
            text += '\n' + url
        rows = [[button('Refresh status', 'status')]]
        if view['status'] == 'choice':
            rows += [[button(str(choice['label'])[:100], 'choose', choice['id'])] for choice in view.get('choices', [])]
        if view['status'] == 'verified':
            rows += [[button('Apply selected cloud storage', 'apply')]]
        await self.display(message, session, text, rows + [[button('Cancel', 'cancel')]])

    async def clear_pending(self, update, context):
        session = self.session_for(update.effective_message)
        if session and not session.busy:
            self.sessions.pop(self.context(update.effective_message), None)
        # Other slash commands continue through the normal gateway group.

    async def input(self, update, context):
        from telegram.ext import ApplicationHandlerStop
        message = update.effective_message
        session = self.session_for(message)
        if not session:
            return
        if session.busy:
            raise ApplicationHandlerStop
        pending = session.pending
        session.busy = True
        try:
            profile = await asyncio.to_thread(self.profile_factory)
            if not self.alive or profile.profile_id != session.binding[-1]:
                self.sessions.pop(self.context(message), None)
                raise ApplicationHandlerStop
            text = message.text
            if pending == 'recipient':
                if not core.RECIPIENT.fullmatch(text):
                    await message.reply_text('Only a public age1 address is accepted. Secrets are not accepted or stored.')
                    raise ApplicationHandlerStop
                await asyncio.to_thread(session.profile.recipient, session.profile.profile_id, text, None)
            elif pending == 'period':
                from .setup_flow import parse_period
                parse_period(text)
                await asyncio.to_thread(session.flow.schedule, text)
            await self.render(message, session)
        except (core.FinixError, ValueError, OSError, RuntimeError):
            await message.reply_text('Finix: input rejected. Interval must be 24h or 7d; key must be public age1 only.')
        finally:
            session.busy = False
        raise ApplicationHandlerStop
