"""Closed owner-command protocol. CSRF tickets never cross this boundary."""
import json
import threading
from urllib.parse import parse_qs, urlsplit

from .connections import Connections, ConnectionError, PROVIDERS, connections, safe_url

MODES = {'local', 'tunnel', 'server-browser'}
HELP = ('connect SERVICE local|tunnel|server-browser confirm; SERVICE: workspace|yandex|onedrive|dropbox. '
        'confirm разрешает широкие права чтения/изменения/удаления диска. '
        'Затем: connect status ID | choose ID NUMBER | apply ID | cancel ID. '
        'Пароли вводите только у провайдера; коды, callback URL, токены и JSON в чат не отправляйте. '
        'Сессия действует 5 минут; apply отдельно, только после verified.')


def public_session(view, mode):
    session = view['session']
    result = {k: session[k] for k in ('id', 'provider', 'status', 'folder', 'choices', 'code') if k in session}
    result['mode'] = mode
    result['instructions'] = 'После входа: connect status ID; после verified: connect apply ID. Отмена: connect cancel ID.'
    url = session.get('url')
    if url and safe_url(session['provider'], url):
        port = 53683 if session['provider'] == 'workspace' else 53682
        if session['provider'] == 'workspace':
            redirect = urlsplit(parse_qs(urlsplit(url).query).get('redirect_uri', [''])[0])
            if redirect.scheme != 'http' or redirect.netloc != '127.0.0.1:53683' or redirect.path != '/':
                raise ConnectionError('UNSAFE_AUTHORIZATION_URL')
        result['url'] = url
        if mode == 'tunnel':
            result['instructions'] = (f'На своём устройстве выполните: ssh -N -L 127.0.0.1:{port}:127.0.0.1:{port} USER@SERVER '
                                      '(замените USER@SERVER своим SSH-адресом). Дождитесь подключения без ошибки '
                                      'занятого порта; не закрывайте SSH до verified. Затем откройте url на этом устройстве. '
                                      'На телефоне нужен собственный SSH-клиент с таким же local forward. '
                                      'Finix не создаёт туннель автоматически.')
        elif mode == 'server-browser':
            result['instructions'] = ('Откройте url браузером на сервере backend (можно попросить агента открыть его). '
                                      'Вход/пароль — только на сайте через защищённое заполнение, не в чате. '
                                      'localhost на телефоне без туннеля не подходит.')
        else:
            result['instructions'] = 'Откройте url браузером на той же машине, где запущен Finix.'
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
