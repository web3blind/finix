"""Credential-owning child. Only closed, value-free events cross stdout.

Never run this through an agent tool for a live account. The UI owns its pipe.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
from urllib.parse import urlsplit


def emit(**event):
    print(json.dumps(event), flush=True)


def read_answer(count):
    line = sys.stdin.readline(100)
    value = json.loads(line)
    if type(value) is not int or not 0 <= value < count:
        raise ValueError('answer')
    return value


def rclone_call(binary, config, args):
    """Capture all rclone output internally, including token-bearing JSON/errors."""
    argv = [binary, '--config', str(config), '--ask-password=false',
            '--contimeout', '15s', '--timeout', '30s', *args]
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    out, line, total = bytearray(), bytearray(), 0
    deadline = time.monotonic() + 300
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(p.stdout, selectors.EVENT_READ, True)
            sel.register(p.stderr, selectors.EVENT_READ, False)
            while sel.get_map():
                if time.monotonic() > deadline:
                    raise ValueError('expired')
                for key, _ in sel.select(.1):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > 131072:
                        raise ValueError('output')
                    if key.data:
                        out.extend(chunk)
                    else:
                        line.extend(chunk)
                        while b'\n' in line:
                            item, _, rest = line.partition(b'\n')
                            line = bytearray(rest)
                            match = re.search(rb'http://127\.0\.0\.1:53682/auth\?state=[A-Za-z0-9_~.%-]+', item)
                            if match:
                                emit(status='authorize', url=match[0].decode())
            p.wait(timeout=5)
        if p.returncode:
            raise ValueError('provider')
        return bytes(out)
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
        p.stdout.close()
        p.stderr.close()


def connect_rclone(binary, stage, provider):
    config = stage / 'rclone.conf'
    config.touch(mode=0o600, exist_ok=False)
    remote = 'finix'
    args = ['config', 'create', remote, provider, 'config_auth_no_browser', 'true', '--non-interactive', '--obscure']
    for _ in range(20):
        result = json.loads(rclone_call(binary, config, args))
        if not isinstance(result, dict) or result.get('Error'):
            raise ValueError('provider')
        state = result.get('State')
        if not state:
            # Prove live read access, not just presence of a token/config file.
            metadata = json.loads(rclone_call(binary, config, ['lsjson', remote + ':', '--stat']))
            if not isinstance(metadata, dict) or metadata.get('IsDir') is not True:
                raise ValueError('probe')
            emit(status='verified')
            return
        option = result.get('Option') or {}
        if option.get('IsPassword'):
            raise ValueError('unsupported')
        name = option.get('Name')
        if name == 'config_is_local':
            answer = 'true'
        elif name == 'config_type':
            # This bounded wizard supports the normal OneDrive route, not
            # SharePoint/site search or free-form remote URLs.
            if not any(e.get('Value') == 'onedrive' for e in option.get('Examples', [])):
                raise ValueError('unsupported')
            answer = 'onedrive'
        elif name == 'config_drive_ok':
            answer = 'true'
        elif name == 'config_driveid':
            examples = option.get('Examples') or []
            if not examples or len(examples) > 30:
                raise ValueError('unsupported')
            # Only bounded disk labels cross; never option help, errors or state.
            labels = []
            for i, example in enumerate(examples):
                label = str(example.get('Help', ''))[:120]
                if any(x in label.lower() for x in ('http', 'token', 'secret', 'code=')):
                    label = ''
                label = ''.join(c for c in label if c.isprintable())
                labels.append({'id': str(i), 'label': label or 'Диск ' + str(i + 1)})
            emit(status='choice', question=name, choices=labels)
            answer = str(examples[read_answer(len(examples))]['Value'])
        else:
            raise ValueError('unsupported')
        args = ['config', 'update', remote, 'config_auth_no_browser', 'true', '--non-interactive', '--continue',
                '--state', state, '--result', answer]
    raise ValueError('steps')


def connect_workspace(home, stage):
    # This subprocess alone owns Google credentials; never forward setup.py's
    # localhost:1/manual-code/argv protocol to the model or plugin settings.
    from google_auth_oauthlib.flow import InstalledAppFlow
    client = home / 'google_client_secret.json'
    if (home / 'google_token.json').exists():
        raise ValueError('reuse existing account')

    # google-auth-oauthlib uses its own WSGI handler; suppress its callback
    # access log (which otherwise includes the authorization code).
    with contextlib.redirect_stderr(io.StringIO()):
        # The client file is owner-controlled, but never let it redirect a
        # Google grant to an arbitrary token server.
        payload = json.loads(client.read_text())
        installed = payload.get('installed') or {}
        if (installed.get('auth_uri') not in {'https://accounts.google.com/o/oauth2/auth',
                                            'https://accounts.google.com/o/oauth2/v2/auth'}
                or installed.get('token_uri') != 'https://oauth2.googleapis.com/token'):
            raise ValueError('client endpoints')
        flow = InstalledAppFlow.from_client_config(
            payload, ['https://www.googleapis.com/auth/drive'])
        flow.run_local_server(host='127.0.0.1', port=0, open_browser=False,
                              authorization_prompt_message='FINIX_AUTH {url}',
                              success_message='Finix: можно закрыть окно.', timeout_seconds=300)
        # Read-only Drive probe before exposing success. No provider data leaves.
        from googleapiclient.discovery import build
        build('drive', 'v3', credentials=flow.credentials).about().get(fields='user').execute()
        fd = os.open(stage / 'workspace-token.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(flow.credentials.to_json())
            stream.flush()
            os.fsync(stream.fileno())
    emit(status='verified')


class SafeStdout:
    """Convert the library's one authorization prompt into a closed event."""
    def __init__(self, original):
        self.original = original

    def write(self, text):
        if text.startswith('FINIX_AUTH '):
            url = text[len('FINIX_AUTH '):].strip()
            parsed = urlsplit(url)
            if parsed.scheme == 'https' and parsed.hostname == 'accounts.google.com':
                self.original.write(json.dumps({'status': 'authorize', 'url': url}) + '\n')
                self.original.flush()
        return len(text)

    def flush(self):
        self.original.flush()


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Hard stop also covers a parent restart, unanswered choices and callbacks.
    import signal
    signal.signal(signal.SIGALRM, lambda *_: os._exit(1))
    signal.alarm(330)
    provider, home, stage, binary = sys.argv[1:]
    try:
        if provider == 'workspace-reuse':
            import importlib.util
            sys.path.insert(0, str(Path(binary).parent))
            spec = importlib.util.spec_from_file_location('finix_workspace_auth', binary)
            module = importlib.util.module_from_spec(spec)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                spec.loader.exec_module(module)
                module._gws_binary = lambda: None
                module.build_service('drive', 'v3').about().get(fields='user').execute()
            emit(status='verified')
        elif provider == 'workspace':
            original = sys.stdout
            sys.stdout = SafeStdout(original)
            try:
                connect_workspace(Path(home), Path(stage))
            finally:
                sys.stdout = original
            # emit inside helper is filtered; final event is explicit.
            emit(status='verified')
        elif provider in {'yandex', 'onedrive', 'dropbox'}:
            connect_rclone(binary, Path(stage), provider)
        else:
            raise ValueError('provider')
    except BaseException:
        emit(status='failed')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
