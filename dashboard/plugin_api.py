"""Mounted by Hermes under /api/plugins/finix with host authentication/profile scope.

Never mount this router on a public unauthenticated standalone server.
"""
from fastapi import APIRouter, HTTPException
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
