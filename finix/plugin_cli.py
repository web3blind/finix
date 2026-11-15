"""Native ``hermes finix`` command, using an explicitly bound local profile."""
import json
import selectors
import sys
import time
import webbrowser

from . import core
from .connect_commands import ConnectCommands, MODES, HELP
from .connections import ConnectionError
from .setup_flow import SetupFlow


def setup_parser(parser):
    parser.description = 'Finix: 1 Key, 2 Cloud, 3 Check and first backup, then schedule.'
    commands = parser.add_subparsers(dest='finix_action')
    key = commands.add_parser('key', help='Report locked public key; never replace an existing key')
    key.add_argument('--recipient', help='Initial public age1 recipient created on your own device')
    commands.add_parser('status', help='Show setup state as JSON')
    commands.add_parser('check', help='Check and run the actual first encrypted backup')
    configure = commands.add_parser('configure', help='Configure existing storage without starting authorization')
    configure.add_argument('key')
    configure.add_argument('value')
    jobs = commands.add_parser('jobs', help='Shared durable backup jobs; start keeps this terminal worker alive')
    operations = jobs.add_subparsers(dest='job_action', required=True)
    start = operations.add_parser('start', help='Emit acknowledgement, then observe until terminal; keep terminal open')
    start.add_argument('--request-id', required=True, help='Canonical UUID; reuse only to replay that intent')
    operations.add_parser('status', help='Read current public job status; never starts work')
    recovery = commands.add_parser('recovery', help='Catalog preview; automatic recovery unavailable pending a public Hermes contract')
    recovery_ops = recovery.add_subparsers(dest='recovery_action', required=True)
    recovery_ops.add_parser('list', help='Discover committed encrypted copies without reading an identity')
    recovery_ops.add_parser('status', help='Read durable SOURCE operation without starting or reconnecting work')
    restore = recovery_ops.add_parser('restore', help='Unavailable: use standalone offline inert restore')
    restore.add_argument('--copy-id', help='Exact id returned by recovery list')
    restore.add_argument('--request-id', help='Canonical intent UUID; exact replay never starts another operation')
    restore.add_argument('--identity', help='Absolute local identity FILE; never key contents')
    restore.add_argument('--acknowledge-incomplete', action='store_true', help='Explicitly accept an incomplete or unknown copy')
    default_prepare = recovery_ops.add_parser('default-prepare', help='Decrypt and seal true-default maintenance; does not stop anything')
    default_prepare.add_argument('--copy-id')
    default_prepare.add_argument('--identity', help='Local identity FILE, never key contents')
    default_prepare.add_argument('--request-id', help='Fresh 32 lowercase hex intent; lost ACK permits exact preview status only')
    default_prepare.add_argument('--consent-prepare', action='store_true')
    for action in ('default-preview','default-preview-cancel'):
        command=recovery_ops.add_parser(action, help='Read or cancel the exact durable preview preparation; never decrypt again')
        command.add_argument('id')
    default_apply = recovery_ops.add_parser('default-apply', help='Separately confirm downtime and launch detached maintenance')
    default_apply.add_argument('id')
    default_apply.add_argument('--confirmation')
    default_apply.add_argument('--confirm', action='store_true')
    default_status = recovery_ops.add_parser('default-status', help='Read durable maintenance status')
    default_status.add_argument('id')
    default_reopen = recovery_ops.add_parser('default-reopen', help='Explicitly continue a dead maintenance worker only')
    default_reopen.add_argument('id')
    default_reopen.add_argument('--confirm', action='store_true')
    schedule = commands.add_parser('schedule', help='Set period after a successful check')
    schedule.add_argument('period', help='Positive whole hours/days, e.g. 24h or 7d')
    connect = commands.add_parser('connect', help='Select local copies or connect a cloud account')
    connect.add_argument('service', nargs='?', choices=('local', 'workspace', 'yandex', 'onedrive', 'dropbox'))
    connect.add_argument('--mode', choices=sorted(MODES))
    connect.add_argument('--confirm', action='store_true', help='Consent to cloud read/write/delete scopes')
    connect.add_argument('--open-browser', action='store_true', help='Open browser on this machine, never in tunnel mode')
    extra = commands.add_parser('extra', help='Owning default-profile external folders')
    extra.add_argument('operation', choices=('list', 'add', 'remove'))
    extra.add_argument('name', nargs='?')
    extra.add_argument('path', nargs='?')


def _emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def _connect(profile, service, mode, confirm=False, open_browser=False):
    if service == 'local':
        profile.configure('storage', 'local')
        _emit(SetupFlow(profile).view())
        return 0
    if not service:
        _emit({'instructions': HELP, 'local': 'hermes finix connect local',
               'example': 'hermes finix connect yandex --mode tunnel --confirm'})
        return 0
    if not confirm or mode not in MODES or (open_browser and mode == 'tunnel'):
        raise ConnectionError('CONSENT_AND_BROWSER_MODE_REQUIRED')
    if not sys.stdin.isatty():
        raise ConnectionError('INTERACTIVE_TERMINAL_REQUIRED; credentials/codes are never accepted on stdin')
    controller = ConnectCommands()
    actor = ('cli', 'local-owner')
    deadline, last, opened = time.monotonic() + 300, None, None
    try:
        event = json.loads(controller.execute(profile, [service, mode, 'confirm'], actor))
        ident = event['id']
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if event != last:
                    _emit(event)
                    last = event
                if event['status'] in {'applied', 'failed', 'cancelled', 'expired'}:
                    return 0 if event['status'] == 'applied' else 1
                if open_browser and event.get('url') and event['url'] != opened:
                    opened = event['url']
                    webbrowser.open(opened)
                if selector.select(.15):
                    line = sys.stdin.readline(128)
                    if not line:
                        return 1
                    words = line.strip().split()
                    if words in (['status'], ['apply'], ['cancel']):
                        action = [words[0], ident]
                    elif len(words) == 2 and words[0] == 'choose' and words[1].isdigit() and len(words[1]) <= 2:
                        action = ['choose', ident, words[1]]
                    else:
                        _emit({'error': 'Only status, choose NUMBER, apply or cancel; never paste codes, credentials or callback URLs.'})
                        continue
                else:
                    action = ['status', ident]
                event = json.loads(controller.execute(profile, action, actor))
        return 1
    finally:
        controller.close()


def _wizard(profile):
    flow = SetupFlow(profile)
    while True:
        print('Finix\n1. Key\n2. Cloud\n3. Check everything and run first backup\n0. Exit', flush=True)
        try:
            choice = input('Choose 1–3: ').strip()
            if choice in {'0', 'q', 'quit'}:
                return 0
            if choice == '1':
                _emit(flow.key())
                print('To register the public key only: hermes finix key --recipient age1…', flush=True)
            elif choice == '2':
                print('1. Local only\n2. Google Workspace\n3. Yandex Disk\n4. OneDrive\n5. Dropbox', flush=True)
                service = {'1': 'local', '2': 'workspace', '3': 'yandex', '4': 'onedrive', '5': 'dropbox'}.get(input('Storage: ').strip())
                if service == 'local':
                    _connect(profile, service, None)
                elif service:
                    mode = input('Browser mode (local/tunnel/server-browser): ').strip()
                    confirm = input('Cloud read/write/delete access: type confirm to proceed: ').strip() == 'confirm'
                    _connect(profile, service, mode, confirm)
            elif choice == '3':
                if input('Run the actual encrypted backup now? Type run: ').strip() != 'run':
                    continue
                _emit(flow.check_and_run())
                period = input('Backup period (24h or 7d; empty leaves scheduling disabled): ').strip()
                if period:
                    _emit(flow.schedule(period))
        except EOFError:
            return 0
        except core.FinixError:
            _emit({'error': 'Setup action failed. Check the public recipient, selected storage, source readiness and period; run check again.'})


def _default_recovery(profile, args):
    from .recovery_availability import capability
    _emit(dict(capability(), activated=False))
    return 1


def _recovery(profile, args):
    from .recovery_availability import capability
    if args.recovery_action not in ('list', 'status'):
        _emit(dict(capability(), activated=False))
        return 1
    from .recovery_http import RecoveryHTTP, binding
    adapter = RecoveryHTTP()
    service = None
    try:
        service = adapter.service(profile)
        if args.recovery_action == 'status':
            from .recovery_operations import RecoveryOperations
            _emit(dict(capability(), retained_operation=RecoveryOperations().current(service)))
        else:
            _emit(dict(profile_id=profile.profile_id,
                       items=service.catalog(profile.profile_id, binding(profile))))
        return 0
    except Exception:
        _emit(dict(error='RECOVERY_FAILED', activated=False))
        return 1
    finally:
        if service is not None:
            service.close()


def run(args, profile_factory=None):
    if getattr(args, 'finix_action', None) == 'recovery' and getattr(args, 'recovery_action', None) not in ('list', 'status'):
        from .recovery_availability import capability
        _emit(dict(capability(), activated=False))
        return 1
    if profile_factory is None:
        from .integration import current_profile
        profile_factory = current_profile
    try:
        profile = profile_factory()
        action = getattr(args, 'finix_action', None)
        if action == 'recovery':
            if getattr(args,'recovery_action','').startswith('default-'):
                return _default_recovery(profile,args)
            return _recovery(profile, args)
        flow = SetupFlow(profile) if action not in {'jobs', 'configure'} else None
        if not action:
            if sys.stdin.isatty() and sys.stdout.isatty():
                return _wizard(profile)
            _emit({**flow.view(), 'help': 'hermes finix key | connect | check | schedule 24h | status | extra; run in a terminal for the numbered wizard'})
        elif action == 'key':
            if args.recipient:
                cfg = profile.config()
                profile.recipient(profile.profile_id, args.recipient, cfg.get('recipient'))
            _emit(flow.key())
        elif action == 'status':
            _emit(flow.view())
        elif action == 'configure':
            _emit(profile.configure(args.key, args.value))
        elif action == 'jobs':
            from .backup_jobs import BackupJobs, _busy
            service = BackupJobs()
            if args.job_action == 'status':
                _emit(service.current(profile))
            else:
                state = service.start(profile, profile.profile_id, args.request_id)
                _emit(state)
                # Do not detach a daemon thread and exit: this CLI is the worker
                # owner. HTTP-hosted workers remain independent of this process.
                while state.get('job') and state['job']['state'] == 'running':
                    time.sleep(.2)
                    observed = service.current(profile)
                    if observed != state:
                        _emit(observed)
                    state = observed
                # Terminal state is persisted immediately before lease release.
                if hasattr(profile, 'root'):
                    while _busy(profile.root / 'backup-jobs' / 'worker.lock'):
                        time.sleep(.01)
                return 0 if state.get('job', {}).get('state') == 'succeeded' else 1
        elif action == 'check':
            _emit(flow.check_and_run())
        elif action == 'schedule':
            _emit(flow.schedule(args.period))
        elif action == 'connect':
            return _connect(profile, args.service, args.mode, args.confirm, args.open_browser)
        elif action == 'extra':
            if ((args.operation == 'list' and (args.name or args.path))
                    or (args.operation == 'add' and (not args.name or not args.path))
                    or (args.operation == 'remove' and (not args.name or args.path))):
                raise core.FinixError('Use extra list | add NAME ABSOLUTE_PATH | remove NAME')
            _emit(profile.extra_sources(args.operation, args.name, args.path))
        return 0
    except ConnectionError as exc:
        _emit({'error': exc.code})
        return 1
    except (core.FinixError, OSError, ValueError, RuntimeError):
        _emit({'error': 'Finix setup failed. Check local configuration, immutable public recipient, selected storage and period. Run check before scheduling.'})
        return 1
