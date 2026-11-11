"""Mounted by Hermes under /api/plugins/finix with host authentication/profile scope.

Never mount this router on a public unauthenticated standalone server.
"""
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
# A directory-installed plugin need not be on sys.path. Load its sibling package
# under this route module's private name, not a global profile-specific singleton.
import importlib.util
import importlib
from pathlib import Path
import sys
import threading
import sqlite3

_setup_lock = threading.Lock()
_setup_running = set()
_setup_errors = {}

_package = __name__ + '_finix'
_spec = importlib.util.spec_from_file_location(
    _package, Path(__file__).parents[1] / 'finix' / '__init__.py',
    submodule_search_locations=[str(Path(__file__).parents[1] / 'finix')])
_module = importlib.util.module_from_spec(_spec)
sys.modules[_package] = _module
_spec.loader.exec_module(_module)
current_profile = importlib.import_module(_package + '.integration').current_profile
FinixError = importlib.import_module(_package + '.core').FinixError
setup_error_code = importlib.import_module(_package + '.setup_errors').setup_error_code
connections = importlib.import_module(_package + '.connections').connections
_backup_jobs = importlib.import_module(_package + '.backup_jobs').BackupJobs()


def setup_flow(profile):
    return importlib.import_module(_package + '.setup_flow').SetupFlow(profile)

router = APIRouter()
# Shares the host's existing authentication and authoritative profile context.
_recovery_api = importlib.import_module(_package + '.recovery_api')
_recovery_router = _recovery_api.create_router(lambda: current_profile())
router.include_router(_recovery_router)

# Bind the same private package used by Connections, not a fresh global import.
# The marker describes loaded code, never current on-disk bytes.
if sys.platform == 'win32':
    _tools = importlib.import_module(_package + '.windows_native')
    _tool_code_version = _tools.TOOL_DISCOVERY_VERSION
else:
    _tools = None
    _tool_code_version = 'canonical-tools-v1'


@router.get('/readiness')
def readiness(response: Response):
    """Public-safe diagnostics under the host's existing auth/profile gate.

    Presence/trust only: no profile config, credentials, spawning or network.
    Never return paths, environment values or exception messages.
    """
    import shutil
    response.headers['Cache-Control'] = 'no-store'
    tools = {}
    for name in ('age', 'age-keygen', 'rclone'):
        try:
            available = bool(_tools.find_tool(name) if _tools else shutil.which(name))
            code = 'READY' if available else 'TOOL_MISSING'
        except FileNotFoundError:
            available, code = False, 'TOOL_MISSING'
        except OSError:
            available, code = False, 'UNTRUSTED_BINARY'
        except (ValueError, RuntimeError):
            available, code = False, 'TOOL_CHECK_FAILED'
        tools[name] = {'available': available, 'code': code}
    return {'platform': sys.platform, 'code_version': _tool_code_version, 'tools': tools}


class Recipient(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: str
    recipient: str
    expected_recipient: str | None


@router.get('/keygen.html')
def keygen_html():
    return FileResponse(Path(__file__).parents[1] / 'dist' / 'keygen.html',
                        media_type='text/html', filename='finix-keygen.html',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.get('/ui-language')
def ui_language(response: Response):
    """Closed, request-profile-scoped display preference; never return config.

    The installed host's read-only loader resolves the authenticated ContextVar
    home and keys its cache by that home. Do not cache this field process-wide.
    """
    response.headers['Cache-Control'] = 'no-store'
    language = None
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
        display = config.get('display') if isinstance(config, dict) else None
        language = display.get('language') if isinstance(display, dict) else None
    except Exception:
        pass  # Closed default; never echo config values, paths or exceptions.
    return {'ui_language': 'en' if isinstance(language, str) and language in ('en', 'en-US', 'en-GB') else 'ru'}


@router.get('/status')
def status():
    return current_profile().status()


@router.post('/recipient')
def recipient(body: Recipient):
    try:
        return current_profile().recipient(body.profile_id, body.recipient, body.expected_recipient)
    except FinixError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


class ConnectionAction(BaseModel):
    model_config = ConfigDict(extra='forbid', str_max_length=500)
    profile_id: str
    ticket: str
    action: str
    provider: str = ''
    session_id: str = ''
    folder: str = 'Finix'
    confirmed: bool = False
    local_browser: bool = False
    choice: str = ''


@router.get('/connection')
def connection(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return connections.view(current_profile())


@router.post('/connection')
def connection_action(body: ConnectionAction, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        profile = current_profile()
        if body.action == 'start' and not profile.config().get('recipient'):
            raise HTTPException(status_code=409, detail='RECIPIENT_REQUIRED')
        return connections.action(profile, **body.model_dump())
    except FinixError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=409, detail='CONNECTION_FAILED') from None


class SetupAction(BaseModel):
    model_config = ConfigDict(extra='forbid', str_max_length=128)
    profile_id: str


class SetupSchedule(SetupAction):
    period: str


class BackupStart(SetupAction):
    request_id: str


@router.get('/backup/jobs/current')
def backup_current(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return _backup_jobs.current(current_profile())
    except (FinixError, OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=409, detail='BACKUP_STATUS_UNAVAILABLE') from None


@router.post('/backup/jobs', status_code=202)
def backup_start(body: BackupStart, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        profile = current_profile()
        if profile.profile_id != body.profile_id:
            raise HTTPException(status_code=409, detail='PROFILE_CHANGED')
        if profile.config().get('storage') == 'local':
            raise HTTPException(status_code=409, detail='CLOUD_SELECTION_REQUIRED')
        return _backup_jobs.start(profile, body.profile_id, body.request_id)
    except FinixError as exc:
        safe = {'PROFILE_CHANGED', 'INVALID_REQUEST_ID', 'JOB_BUSY', 'JOB_CAPACITY', 'JOB_HISTORY_FULL'}
        code = str(exc) if str(exc) in safe else 'BACKUP_START_FAILED'
        raise HTTPException(status_code=409, detail=code) from None
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=409, detail='BACKUP_START_FAILED') from None


@router.get('/setup')
def setup_view(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        profile = current_profile()
        result = setup_flow(profile).view()
        with _setup_lock:
            result['check_running'] = profile.profile_id in _setup_running
            result['last_error_code'] = _setup_errors.get(profile.profile_id)
        return result
    except (FinixError, OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=409, detail='SETUP_STATUS_FAILED') from None


def setup_target(profile_id, profile=None):
    profile = profile if profile is not None else current_profile()
    if profile.profile_id != profile_id:
        raise HTTPException(status_code=409, detail='PROFILE_CHANGED')
    return setup_flow(profile)


@router.post('/setup/check')
def setup_check(body: SetupAction, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    profile = current_profile()
    flow = setup_target(body.profile_id, profile)
    if profile.config().get('storage') == 'local':
        raise HTTPException(status_code=409, detail='CLOUD_SELECTION_REQUIRED')
    with _setup_lock:
        if body.profile_id in _setup_running:
            raise HTTPException(status_code=409, detail='SETUP_CHECK_RUNNING')
        _setup_running.add(body.profile_id)
        _setup_errors.pop(body.profile_id, None)
    try:
        return flow.check_and_run()
    except (FinixError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        code = setup_error_code(exc)
        with _setup_lock:
            _setup_errors[body.profile_id] = code
        raise HTTPException(status_code=409, detail=code) from None
    finally:
        with _setup_lock:
            _setup_running.discard(body.profile_id)


@router.post('/setup/schedule')
def setup_schedule(body: SetupSchedule, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    import re
    if not re.fullmatch(r'[1-9][0-9]*[hd]', body.period):
        raise HTTPException(status_code=422, detail='PERIOD_FORMAT_24h_OR_7d')
    try:
        return setup_target(body.profile_id).schedule(body.period)
    except (FinixError, OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=409, detail='SETUP_SCHEDULE_FAILED') from None
