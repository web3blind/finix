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
        return connections.action(current_profile(), **body.model_dump())
    except FinixError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=409, detail='CONNECTION_FAILED') from None
