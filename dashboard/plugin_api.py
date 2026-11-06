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

_setup_lock = threading.Lock()
_setup_running = set()

_package = __name__ + '_finix'
_spec = importlib.util.spec_from_file_location(
    _package, Path(__file__).parents[1] / 'finix' / '__init__.py',
    submodule_search_locations=[str(Path(__file__).parents[1] / 'finix')])
_module = importlib.util.module_from_spec(_spec)
sys.modules[_package] = _module
_spec.loader.exec_module(_module)
current_profile = importlib.import_module(_package + '.integration').current_profile
FinixError = importlib.import_module(_package + '.core').FinixError
connections = importlib.import_module(_package + '.connections').connections


def setup_flow(profile):
    return importlib.import_module(_package + '.setup_flow').SetupFlow(profile)

router = APIRouter()


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


@router.get('/setup')
def setup_view(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        profile = current_profile()
        result = setup_flow(profile).view()
        with _setup_lock:
            result['check_running'] = profile.profile_id in _setup_running
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
    try:
        return flow.check_and_run()
    except (FinixError, OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=409, detail='SETUP_CHECK_FAILED') from None
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
