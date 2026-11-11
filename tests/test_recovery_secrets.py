"""Inert schema preservation: no source registration or external fetch."""
import pytest
import yaml
from finix.recovery_import import _safe_config, RecoveryImportError


def test_source_order_preserved_while_providers_parked():
    sources = ['onepassword', 'bitwarden']
    config = {'model': {'default': 'fixture'}, 'secrets': {'sources': sources,
        'onepassword': {'enabled': True, 'account': 'fixture'},
        'bitwarden': {'enabled': True, 'mapping': {'KEY': 'fixture'}}}}
    out = yaml.safe_load(_safe_config(yaml.safe_dump(config)))
    assert out['secrets']['sources'] == sources
    assert out['secrets']['onepassword'] == {'enabled': False, 'account': 'fixture'}
    assert out['secrets']['bitwarden']['enabled'] is False


@pytest.mark.parametrize('secrets', [{'sources':'onepassword'}, {'sources':{}},
    {'sources':[1]}, {'sources':[True]}, {'sources':[{}]}, {'onepassword':True}, {'onepassword':[]}])
def test_malformed_secret_metadata_rejected(secrets):
    with pytest.raises(RecoveryImportError, match='INVALID_CONFIG'):
        _safe_config(yaml.safe_dump({'model':'fixture', 'secrets':secrets}))
