from types import SimpleNamespace
from unittest.mock import patch
import pytest
from finix import recovery_import as module


def test_changed_host_source_is_closed_before_publication():
    host = SimpleNamespace(import_profile=lambda: None)
    with patch('inspect.getsource', return_value='def import_profile():\n    pass\n'):
        with pytest.raises(module.RecoveryImportError, match='HOST_UNAVAILABLE'):
            module.verify_host_compatibility(host)


def test_declared_sdk_contract_is_exact_version_pin():
    assert module.DESKTOP_SDK_CONTRACT_VERSION == '0.23.0'
    assert set(module.HOST_SOURCE_PINS) == {'import_profile','safe_extract_targz','archive_root_dirs','get_profile_dir','_canon_valid'}
