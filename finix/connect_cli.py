"""Single-process interactive connection; no secret-bearing input protocol."""
import json
from pathlib import Path
import selectors
import sys
import time
import webbrowser

from .connections import ConnectionError
from .connect_commands import ConnectCommands, MODES, HELP
from .integration import Profile


def add_parser(commands):
    parser = commands.add_parser('connect', help='Interactive cloud connection; credentials stay in the owning worker')
    parser.add_argument('service', choices=('workspace', 'yandex', 'onedrive', 'dropbox'))
    parser.add_argument('--home', required=True, help='Explicit existing owning Hermes profile directory')
    parser.add_argument('--mode', required=True, choices=sorted(MODES))
    parser.add_argument('--confirm', action='store_true', help='Consent to potentially broad cloud read/write/delete scopes')
    parser.add_argument('--open-browser', action='store_true', help='Explicitly open the browser on THIS machine; not for tunnel mode')


def interactive(service, home, mode, confirm=False, open_browser=False):
    if not confirm or (open_browser and mode == 'tunnel'):
        raise ConnectionError('CONSENT_AND_BROWSER_MODE_REQUIRED; ' + HELP)
    if not Path(home).is_absolute() or not Path(home).is_dir():
        raise ConnectionError('EXISTING_PROFILE_REQUIRED')
    profile, controller = Profile(home), ConnectCommands()
    actor = ('cli', 'local-owner')
    last, opened = None, None
    deadline = time.monotonic() + 300
    try:
        event = json.loads(controller.execute(profile, [service, mode, 'confirm'], actor))
        ident = event['id']
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if event != last:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                    last = event
                if event['status'] == 'applied':
                    return 0
                if event['status'] in {'failed', 'cancelled', 'expired'}:
                    return 1
                if open_browser and event.get('url') and opened != event['url']:
                    opened = event['url']
                    webbrowser.open(opened)
                if selector.select(.15):
                    line = sys.stdin.readline(128)
                    if not line:
                        print(controller.execute(profile, ['cancel', ident], actor), flush=True)
                        return 1
                    words = line.strip().split()
                    if words == ['apply'] or words == ['cancel'] or words == ['status']:
                        action = [words[0], ident]
                    elif len(words) == 2 and words[0] == 'choose' and words[1].isdigit() and len(words[1]) <= 2:
                        action = ['choose', ident, words[1]]
                    else:
                        print(json.dumps({'error': 'Only status, choose NUMBER, apply or cancel; never paste codes or URLs.'}), flush=True)
                        continue
                    try:
                        event = json.loads(controller.execute(profile, action, actor))
                    except ConnectionError as exc:
                        print(json.dumps({'error': exc.code}), flush=True)
                else:
                    event = json.loads(controller.execute(profile, ['status', ident], actor))
        return 1
    finally:
        controller.close()
