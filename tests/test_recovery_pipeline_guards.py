import os
from pathlib import Path
import pytest


def test_unregistered_bootstrap_capability_refused_before_sdk_start():
    from finix.recovery_pipeline import _bootstrap
    from finix.recovery_import import RecoveryImportError
    with pytest.raises(RecoveryImportError,match='IMPORT_REQUIRED'):
        _bootstrap(object())


def test_wrong_capability_purpose_refused_before_sdk_start():
    from finix.recovery_pipeline import _CAPS,_bootstrap
    from finix.recovery_import import RecoveryImportError
    cap=object();_CAPS[cap]={'purpose':'client-boolean-bypass'}
    try:
        with pytest.raises(RecoveryImportError,match='IMPORT_REQUIRED'):
            _bootstrap(cap)
    finally:_CAPS.pop(cap)


def test_native_identity_probe_does_not_reacquire_retained_directory_lock(tmp_path):
    from finix import core
    from finix.recovery_pipeline import _identity
    path=core.private_dir(tmp_path/'private')
    with core.locked(path):
        actual=_identity(path)
    info=path.stat()
    if os.name=='nt':
        from finix.windows_native import canonical_directory
        assert actual==canonical_directory(path)[1]
    else:assert actual==(info.st_dev,info.st_ino)
