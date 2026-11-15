"""Read-only recovery history stays separate from retired worker admission."""
from contextlib import nullcontext
import json
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from finix import recovery_http


def client_for(monkeypatch, profile, service):
    monkeypatch.setattr(recovery_http, 'local_request', lambda request: True)
    monkeypatch.setattr(recovery_http.RecoveryHTTP, 'service', lambda self, p: service)
    app = FastAPI()
    app.include_router(recovery_http.create_router(lambda: profile))
    return TestClient(app)


def test_catalog_reads_with_retained_running_jobs_without_rewriting(monkeypatch, tmp_path):
    home, state = tmp_path / 'home', tmp_path / 'state'
    state.mkdir()
    records = []
    for relative in ('recovery-operations/current.json', 'default-preview/current.json'):
        path = home / 'cache/finix' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"state":"running","id":"retained"}')
        records.append((path, path.read_bytes()))
    profile = SimpleNamespace(home=home, profile_id='a' * 64, directories=lambda: state)
    service = SimpleNamespace(activation='fixture', _catalog={}, catalog=Mock(return_value=[]))
    lock = Mock(side_effect=lambda path: nullcontext())
    monkeypatch.setattr(recovery_http.core, 'locked', lock)
    client = client_for(monkeypatch, profile, service)
    response = client.get('/recovery/catalog')
    assert response.status_code == 200
    assert response.json() == {'profile_id': profile.profile_id, 'items': []}
    lock.assert_called_once_with(state)
    service.catalog.assert_called_once_with(profile.profile_id, 'fixture')
    assert all(path.read_bytes() == before for path, before in records)


def test_retained_operation_is_historical_not_cancellable(monkeypatch, tmp_path):
    from finix import recovery_operations
    home = tmp_path / 'home'
    from finix import core
    root = core.private_dir(home / 'cache/finix/recovery-operations')
    path = root / 'current.json'
    saved = {'id': 'b' * 32, 'profile_id': 'a' * 64, 'activation': 'fixture',
             'state': 'running', 'cancellable': True, 'full_acceptance': True}
    path.write_text(json.dumps(saved), encoding='utf-8')
    before = path.read_bytes()
    service = SimpleNamespace(profile=home, profile_id='a' * 64, activation='fixture', _fence=Mock())
    monkeypatch.setattr(recovery_operations, 'state_lease', lambda path: nullcontext())
    public = recovery_operations.RecoveryOperations().current(service)
    assert public is not None
    assert public['state'] == 'running'
    assert public['historical_only'] is True
    assert public['runtime_capability_supported'] is False
    assert public['cancellable'] is False
    assert public['full_acceptance'] is False
    assert path.read_bytes() == before


def test_removed_cloud_transport_keeps_closed_actionable_code(monkeypatch, tmp_path):
    from finix.workspace_drive import RemovedWorkspaceTransport
    state = tmp_path / 'state'
    state.mkdir()
    profile = SimpleNamespace(profile_id='a' * 64, directories=lambda: state)
    service = SimpleNamespace(activation='fixture', _catalog={},
                              catalog=Mock(side_effect=RemovedWorkspaceTransport()))
    monkeypatch.setattr(recovery_http.core, 'locked', lambda path: nullcontext())
    response = client_for(monkeypatch, profile, service).get('/recovery/catalog')
    assert response.status_code == 409
    assert response.json() == {'detail': 'WORKSPACE_SKILL_TRANSPORT_REMOVED'}
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('path', ['/recovery/capabilities', '/recovery/default/capabilities',
                                  '/recovery/catalog', '/recovery/default/apply'])
def test_profile_failure_has_closed_no_store_response(monkeypatch, path):
    monkeypatch.setattr(recovery_http, 'local_request', lambda request: True)
    def broken():
        raise RuntimeError('PRIVATE-SENTINEL')
    app = FastAPI()
    app.include_router(recovery_http.create_router(broken))
    client = TestClient(app)
    response = client.post(path, content=b'not-a-key') if path.endswith('/apply') else client.get(path)
    assert response.status_code == 409
    assert response.json() == {'detail': 'PROFILE_UNAVAILABLE'}
    assert response.headers['cache-control'] == 'no-store'
    assert 'PRIVATE-SENTINEL' not in response.text
