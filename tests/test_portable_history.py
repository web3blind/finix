"""New-copy contract: original authority is never replayed."""
import json
import pytest
from pathlib import Path
from test_recovery_history_ownership import fenced_root, seed


def test_prepare_new_copy_without_source_stamp(tmp_path):
    from finix.portable_history import prepare
    from finix import core
    src=seed(tmp_path); before=src.read_bytes()
    stage=core.private_dir(tmp_path/'portable')
    plan=prepare(src,stage,expected_payload={'size':len(before),'sha256':core.digest_file(src)})
    assert plan['contract']=='sdk-new-content-copy-v1'
    assert len(plan['sessions'])==3
    assert sum(len(s['messages']) for s in plan['sessions'])==12
    assert src.read_bytes()==before
    assert (stage/'original.db').read_bytes()==before
    assert 'sessions' in json.loads((stage/'census.json').read_text())


@pytest.mark.parametrize('rows',[
    [{'id':'a','parent_session_id':'missing'}],
    [{'id':'a','parent_session_id':'b'},{'id':'b','parent_session_id':'a'}],
    [{'id':'a'},{'id':'a'}]])
def test_bad_lineage_and_collisions_refuse(rows):
    from finix.portable_history import ordered
    from finix.recovery_import import RecoveryImportError
    with pytest.raises(RecoveryImportError,match='HISTORY_INVALID_LINEAGE|HISTORY_ID_COLLISION'):
        ordered(rows)


def test_payload_substitution_refuses(tmp_path):
    from finix.portable_history import prepare
    from finix.recovery_import import RecoveryImportError
    from finix import core
    src=seed(tmp_path)
    with pytest.raises(RecoveryImportError,match='HISTORY_SOURCE_BINDING_REQUIRED'):
        prepare(src,core.private_dir(tmp_path/'portable'),expected_payload={'size':src.stat().st_size,'sha256':'0'*64})
