"""Closed owner-command protocol. CSRF tickets never cross this boundary."""
import json
import threading
from urllib.parse import parse_qs, urlsplit

from .connections import Connections, ConnectionError, PROVIDERS, connections, safe_url

MODES = {'local', 'tunnel', 'server-browser'}
HELP = ('connect SERVICE local|tunnel|server-browser confirm; SERVICE: workspace|workspace-skill|yandex|onedrive|dropbox. '
        'Confirm the requested provider permissions. workspace uses hermes auth add finix:google. '
        'workspace-skill requires the compatible Google Workspace skill already authorized in this same profile; '
        'Finix uses the skill API without reading or copying credentials. '
        'Then: connect status ID | choose ID NUMBER | apply ID | cancel ID. '
        'Enter passwords only at the provider; never send codes, callback URLs, tokens or JSON in chat. '
        'Sessions expire after five minutes; apply is explicit and only available after verification.')


def public_session(view, mode):
    session = view['session']
    result = {k: session[k] for k in ('id', 'provider', 'status', 'folder', 'choices', 'code') if k in session}
    result['mode'] = mode
    result['instructions'] = 'After sign-in: connect status ID; after verified: connect apply ID. Cancel: connect cancel ID.'
    url = session.get('url')
    if url and safe_url(session['provider'], url):
        port = 53682
        result['url'] = url
        if mode == 'tunnel':
            result['instructions'] = (f'Run on your device: ssh -N -L 127.0.0.1:{port}:127.0.0.1:{port} USER@SERVER '
                                      '(replace USER@SERVER with your SSH address). Wait for a successful connection with no '
                                      'port conflict; keep SSH open until verified, then open the URL on this device. '
                                      'A phone requires its own SSH client with the same local forward. '
                                      'Finix never creates tunnels automatically.')
        elif mode == 'server-browser':
            result['instructions'] = ('Open the URL in a browser on the backend server. '
                                      'Enter credentials only on the site using secure filling, never in chat. '
                                      'A phone localhost URL cannot reach the server without a tunnel.')
        else:
            result['instructions'] = 'Open the URL in a browser on the same machine as Finix.'
    return result


class ConnectCommands:
    def __init__(self, manager=None):
        self.manager = manager if manager is not None else Connections()
        self.lock = threading.RLock()
        self.bindings = {}
        self.closed = False

    def execute(self, profile, args, actor):
        with self.lock:
            if self.closed:
                raise ConnectionError('PLUGIN_UNLOADED')
            if not args:
                return HELP
            view = self.manager.view(profile)
            if len(args) == 3 and args[0] in PROVIDERS and args[1] in MODES and args[2] == 'confirm':
                view = self.manager.action(profile, profile_id=profile.profile_id, ticket=view['ticket'],
                                           action='start', provider=args[0], confirmed=True, local_browser=True)
                ident = view['session']['id']
                self.bindings[profile.profile_id] = (ident, actor, args[1], profile)
                return json.dumps(public_session(view, args[1]), ensure_ascii=False)
            if len(args) not in {2, 3} or args[0] not in {'status', 'choose', 'apply', 'cancel'}:
                raise ConnectionError('USAGE; ' + HELP)
            action, ident = args[:2]
            if len(args) != (3 if action == 'choose' else 2):
                raise ConnectionError('INVALID_ARGUMENTS')
            binding = self.bindings.get(profile.profile_id)
            if not binding or binding[:2] != (ident, actor) or view['session'].get('id') != ident:
                raise ConnectionError('SESSION_CHANGED')
            if action != 'status':
                view = self.manager.action(profile, profile_id=profile.profile_id, ticket=view['ticket'],
                                           action='answer' if action == 'choose' else action,
                                           session_id=ident, choice=args[2] if action == 'choose' else '')
            return json.dumps(public_session(view, binding[2]), ensure_ascii=False)

    def close(self):
        with self.lock:
            self.closed = True
            with self.manager.lock:
                for profile_id, (ident, _, _, _) in self.bindings.items():
                    session = self.manager.sessions.get(profile_id)
                    if session and session['public'].get('id') == ident and session['public']['status'] != 'applied':
                        self.manager._stop(session, 'cancelled')
                self.bindings.clear()


# Direct library calls retain state too; native registration owns its controller.
default_commands = ConnectCommands(connections)
