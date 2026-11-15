"""Behavior contracts for the retired runtime boundary, independent of SDK source."""
from types import SimpleNamespace
import pytest
from finix.recovery_availability import CODE
from finix.recovery_import import RecoveryImportError


class NoAccess:
    def __getattribute__(self, name):
        raise AssertionError('unavailable boundary accessed ' + name)


def test_cli_refuses_before_home_or_key_access(capsys):
    from finix.plugin_cli import _recovery, _default_recovery
    assert _recovery(NoAccess(), SimpleNamespace(recovery_action='restore')) == 1
    assert _default_recovery(NoAccess(), NoAccess()) == 1
    assert capsys.readouterr().out.count(CODE) == 2


def test_default_api_never_adopts_old_plan(tmp_path):
    from finix.default_recovery_api import DefaultRecoveryAPI, RecoveryError
    retained = tmp_path / 'pending.json'
    retained.write_bytes(b'{"state":"publishing","journal":"retained"}')
    before = retained.read_bytes()
    api = DefaultRecoveryAPI()
    for call in (lambda:api.prepare(NoAccess(), NoAccess(), 'old', consent=True),
                 lambda:api.apply(NoAccess(), 'old', 'old', consent=True),
                 lambda:api.reopen(NoAccess(), 'old', consent=True)):
        with pytest.raises(RecoveryError, match=CODE):
            call()
    assert retained.read_bytes() == before


def test_operation_refuses_and_wipes_supplied_key_without_service_access():
    from finix.recovery_operations import RecoveryOperations
    owner = RecoveryOperations()
    with pytest.raises(RecoveryImportError, match=CODE):
        owner.begin(NoAccess(), request_id='old', candidate_id='old')
    identity = bytearray(b'synthetic-test-key')
    with pytest.raises(RecoveryImportError, match=CODE):
        owner.key(NoAccess(), 'old', request_id='old', identity=identity)
    assert identity == bytearray(len(identity))


@pytest.mark.parametrize('path', ['/operations', '/operations/old/key', '/default/prepare-auto',
                                  '/default/prepare', '/default/apply', '/default/reopen',
                                  '/jobs/old/import', '/jobs/old/import-auto', '/jobs/old/start'])
def test_http_refuses_before_body_read(path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from finix import recovery_http
    monkeypatch.setattr(recovery_http, 'local_request', lambda request: True)
    async def forbidden(*args, **kwargs):
        raise AssertionError('read request body before capability refusal')
    monkeypatch.setattr(recovery_http, 'bounded_body', forbidden)
    app = FastAPI()
    app.include_router(recovery_http.create_router(lambda:SimpleNamespace(profile_id='a'*64)))
    response = TestClient(app).post('/recovery'+path, content=b'not-a-key')
    assert response.status_code == 409
    assert response.json() == {'detail': CODE}


def test_inert_result_stays_successful_but_not_importable(tmp_path):
    from finix.recovery_contract import public_job
    (tmp_path/'config.yaml').write_text('model: fixture\n')
    service = SimpleNamespace(activation='fixture', handoff=lambda *args:{
        'bundle_root':str(tmp_path), 'profile_root':str(tmp_path)})
    result = public_job(service, 'job', 'profile', {'status':'succeeded',
        'result':{'complete':True, 'omitted_count':0}})
    assert result['archive_verified'] is True
    assert result['completion_scope'] == 'archive'
    assert result['can_import'] is False
    assert result['backend_target_ready'] is False


def test_inert_config_verification_still_parks_integrations():
    import yaml
    from finix.recovery_import import _safe_config
    result = yaml.safe_load(_safe_config(b'model: fixture\nplugins:\n  enabled: [archived]\nplatforms:\n  telegram:\n    enabled: true\n'))
    assert result['plugins']['enabled'] == []
    assert result['platforms']['telegram']['enabled'] is False
