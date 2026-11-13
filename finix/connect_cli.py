"""Single-process interactive connection; no secret-bearing input protocol."""
import json
from pathlib import Path
import os
import select
import sys
import time
import webbrowser

from .connections import ConnectionError
from .connect_commands import ConnectCommands, MODES, HELP
from .integration import Profile


class _StdinReader:
    """Bounded, nonblocking command lines; never owns/closes stdin.

    None means no complete line yet, '' means EOF. Overlong lines are
    discarded through their newline and returned as an invalid command.
    No threads or buffered readline calls can outlive the interaction.
    """

    def __init__(self, stream):
        self.fd = stream.fileno()
        self.line = bytearray()
        self.overflow = False
        self.eof = False
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes
            self.handle = msvcrt.get_osfhandle(self.fd)
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                                wintypes.DWORD, wintypes.LPVOID,
                                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            self.kernel.PeekNamedPipe.restype = wintypes.BOOL
            self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
            self.kernel.GetFileType.restype = wintypes.DWORD
            self.kind = self.kernel.GetFileType(self.handle)

    def _char(self):
        if os.name != 'nt':
            if not select.select([self.fd], [], [], 0)[0]:
                return None
        elif self.kind == 2:  # FILE_TYPE_CHAR: never block in console readline
            import msvcrt
            if not msvcrt.kbhit():
                return None
            # getwch deliberately does not echo arbitrary pasted input.
            char = msvcrt.getwch()
            if getattr(self, 'extended_key', False):
                self.extended_key = False
                return False
            if char in ('\x00', '\xe0'):
                self.extended_key = True
                return False
            if char == '\x03':
                raise KeyboardInterrupt
            if char == '\x1a':
                return b''
            if char == '\b':
                if self.line and not self.overflow:
                    self.line.pop()
                return False
            return ('\n' if char == '\r' else char).encode('utf-8', errors='replace')
        elif self.kind == 3:  # FILE_TYPE_PIPE, including ConPTY's pipe side
            import ctypes
            from ctypes import wintypes
            available = wintypes.DWORD()
            if not self.kernel.PeekNamedPipe(self.handle, None, 0, None,
                                            ctypes.byref(available), None):
                error = ctypes.get_last_error()
                if error in (109, 232):  # broken/disconnected pipe
                    return b''
                raise ctypes.WinError(error)
            if not available.value:
                return None
        return os.read(self.fd, 1)

    def poll(self, timeout):
        deadline = time.monotonic() + timeout
        # A byte budget also bounds work when a producer floods invalid input.
        for _ in range(256):
            char = b'' if self.eof else self._char()
            if char is False:  # consumed a console editing/special key
                continue
            if char is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                time.sleep(min(.01, remaining))
                continue
            if char in (b'', b'\n'):
                self.eof = char == b''
                line = 'INVALID' if self.overflow else self.line.decode('utf-8', errors='replace')
                self.line.clear()
                self.overflow = False
                return line + '\n' if char or line else ''
            if len(self.line) < 128:
                self.line.extend(char)
            else:
                self.overflow = True
        return None


def add_parser(commands):
    parser = commands.add_parser('connect', help='Interactive cloud connection; credentials stay in the owning worker')
    from .connections import PROVIDERS
    parser.add_argument('service', choices=tuple(PROVIDERS))
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
        reader = _StdinReader(sys.stdin)
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
            line = reader.poll(.15)
            if line is not None:
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
